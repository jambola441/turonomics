"""The connect-a-mailbox handshake.

The app has no sign-in of its own yet, so the callback is reachable by anyone
who finds it. What stops that mattering is the signed state and the address
allowlist, and those are what these tests are mostly about. The token-lifecycle
tests cover the one Google behaviour that silently breaks a grant: a refresh
response omits ``refresh_token``, and treating that as "no refresh token" logs
the mailbox out an hour after connecting.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select

from turonomics_api.db.models import OAuthToken
from turonomics_api.gmail.client import (
    PROVIDER,
    GmailClient,
    GmailConfig,
    GmailError,
    GmailNotConnected,
    authorize_url,
)

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL") and not os.environ.get("DATABASE_URL"),
    reason="no database configured",
)

CONFIG = GmailConfig(
    client_id="cid",
    client_secret="sekrit",
    redirect_uri="https://turonomics.onrender.com/api/gmail/callback",
    address="jambola441@gmail.com",
)


def _http(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


# ---------------------------------------------------------------------------
# Getting a refresh token at all
# ---------------------------------------------------------------------------


def test_the_consent_url_asks_for_offline_access_and_forces_the_prompt():
    """Without both, Google returns an access token and no refresh token, and
    the grant quietly lasts an hour instead of indefinitely."""
    url = authorize_url(CONFIG, state="st")
    assert "access_type=offline" in url
    assert "prompt=consent" in url
    assert "gmail.readonly" in url
    assert "state=st" in url


def test_an_exchange_that_returns_no_refresh_token_is_an_error_not_a_grant(session):
    """Storing it would look like success and fail an hour later, somewhere
    else, with a message that had nothing to do with the cause."""

    def handler(request):
        return httpx.Response(200, json={"access_token": "a", "expires_in": 3600})

    with pytest.raises(GmailError, match="no refresh token"):
        GmailClient(session, CONFIG, _http(handler)).exchange_code("code")


def test_exchanging_the_code_stores_the_grant(session):
    def handler(request):
        return httpx.Response(
            200, json={"access_token": "at", "refresh_token": "rt", "expires_in": 3600}
        )

    GmailClient(session, CONFIG, _http(handler)).exchange_code("code")
    session.commit()

    row = session.scalar(select(OAuthToken).where(OAuthToken.provider == PROVIDER))
    assert row.access_token == "at"
    assert row.refresh_token == "rt"


# ---------------------------------------------------------------------------
# Keeping it
# ---------------------------------------------------------------------------


def test_a_refresh_response_without_a_refresh_token_keeps_the_stored_one(session):
    """Google omits it on every ordinary refresh. Treating the omission as a
    revocation would disconnect the mailbox an hour after connecting."""
    session.add(
        OAuthToken(
            provider=PROVIDER,
            access_token="old",
            refresh_token="rt",
            expires_at=datetime.now(UTC) - timedelta(minutes=5),
        )
    )
    session.commit()

    def handler(request):
        return httpx.Response(200, json={"access_token": "new", "expires_in": 3600})

    token = GmailClient(session, CONFIG, _http(handler)).access_token()
    session.commit()

    assert token == "new"
    row = session.scalar(select(OAuthToken).where(OAuthToken.provider == PROVIDER))
    assert row.refresh_token == "rt", "the stored refresh token must survive"
    assert row.refresh_count == 1


def test_a_live_token_is_not_refreshed(session):
    session.add(
        OAuthToken(
            provider=PROVIDER,
            access_token="still-good",
            refresh_token="rt",
            expires_at=datetime.now(UTC) + timedelta(minutes=30),
        )
    )
    session.commit()

    def explode(request):  # pragma: no cover - must not be called
        raise AssertionError("refreshed a token that had not expired")

    assert GmailClient(session, CONFIG, _http(explode)).access_token() == "still-good"


def test_a_revoked_grant_says_reconnect_rather_than_retrying_forever(session):
    session.add(
        OAuthToken(
            provider=PROVIDER,
            access_token="old",
            refresh_token="revoked",
            expires_at=datetime.now(UTC) - timedelta(minutes=5),
        )
    )
    session.commit()

    def handler(request):
        return httpx.Response(400, json={"error": "invalid_grant"})

    with pytest.raises(GmailNotConnected, match="reconnect"):
        GmailClient(session, CONFIG, _http(handler)).access_token()


def test_reading_mail_before_connecting_is_a_clear_failure(session):
    with pytest.raises(GmailNotConnected, match="not connected"):
        GmailClient(session, CONFIG, _http(lambda r: httpx.Response(200))).search("from:turo")


# ---------------------------------------------------------------------------
# The callback, which anyone can reach
# ---------------------------------------------------------------------------


def test_the_callback_refuses_a_state_it_did_not_sign(client, monkeypatch):
    """Google hands back whatever state it was given, so an unverified state
    means the callback would act on a flow this server never started."""
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "sekrit")
    r = client.get("/api/gmail/callback?code=x&state=forged", follow_redirects=False)
    assert r.status_code == 400


def test_the_callback_refuses_a_state_signed_with_a_different_secret(client, monkeypatch):
    from turonomics_api.routers.gmail import _sign_state

    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "one-secret")
    state = _sign_state()
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "another-secret")
    r = client.get(f"/api/gmail/callback?code=x&state={state}", follow_redirects=False)
    assert r.status_code == 400


def test_an_expired_state_is_refused(client, monkeypatch):
    import turonomics_api.routers.gmail as mod

    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "sekrit")
    monkeypatch.setattr(mod.time, "time", lambda: 1_000_000.0)
    state = mod._sign_state()
    monkeypatch.setattr(mod.time, "time", lambda: 1_000_000.0 + mod.STATE_TTL_SECONDS + 1)
    r = client.get(f"/api/gmail/callback?code=x&state={state}", follow_redirects=False)
    assert r.status_code == 400


def test_cancelling_at_googles_screen_is_not_a_server_error(client, monkeypatch):
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "sekrit")
    r = client.get("/api/gmail/callback?error=access_denied", follow_redirects=False)
    assert r.status_code == 400


def test_connect_reports_missing_configuration_rather_than_crashing(client, monkeypatch):
    monkeypatch.delenv("GOOGLE_CLIENT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_CLIENT_SECRET", raising=False)
    assert client.get("/api/gmail/connect", follow_redirects=False).status_code == 503


def test_status_reports_not_connected_before_anyone_has_connected(api_client):
    body = api_client.get("/api/gmail/status").json()
    assert body["connected"] is False
    assert body["address"] is None
