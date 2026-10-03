"""Bouncie client and sync tests.

Runs against a mocked transport so CI needs no credentials. The shapes asserted
here were taken from real responses, not from the spec alone.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import func, select

from turonomics_api.bouncie.client import BouncieClient, BouncieConfig, BouncieError
from turonomics_api.bouncie.sync import sync_vehicles
from turonomics_api.db.models import OAuthToken, TelemetryEvent, Vehicle

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL") and not os.environ.get("DATABASE_URL"),
    reason="no database configured",
)

CONFIG = BouncieConfig(
    client_id="cid",
    client_secret="secret",
    auth_code="code",
    redirect_uri="http://localhost:8000/auth/bouncie/callback",
)

VEHICLES = [
    {
        "nickName": "Jolene",
        "model": {"make": "TOYOTA", "name": "Corolla", "year": 2025},
        "vin": "VINJOLENE00000001",
        "imei": "111111111111111",
        "standardEngine": "2L I4",
        "stats": {
            "localTimeZone": "-0400",
            "odometer": 23196.4,
            "lastUpdated": "2026-09-14T13:38:38.000Z",
            "location": {
                "lat": 40.678716,
                "lon": -73.971175,
                "heading": 213,
                "address": "164 Saint Marks Ave, Brooklyn, NY",
            },
            "fuelLevel": 92.5,
            "isRunning": False,
            "speed": 0,
            "mil": {"milOn": False, "qualifiedDtcList": []},
            "battery": {"status": "normal"},
        },
    },
    {
        # A vehicle whose OBD reports neither fuel nor odometer: check-out must
        # fall back to manual entry for this one.
        "nickName": "Sparse",
        "model": {"make": "FORD", "name": "Transit", "year": 2024},
        "vin": "VINSPARSE00000002",
        "imei": "222222222222222",
        "stats": {
            "lastUpdated": "2026-09-15T11:24:30.000Z",
            "location": {
                "lat": 40.679865,
                "lon": -73.970204,
                "heading": 301,
                "address": "590 Bergen St, Brooklyn, NY",
            },
        },
    },
]


def _transport(token_responses, calls):
    """A transport that records requests and replays scripted token responses."""

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path == "/oauth/token":
            status, body = token_responses.pop(0)
            return httpx.Response(status, json=body)
        if request.url.path == "/v1/vehicles":
            return httpx.Response(200, json=VEHICLES)
        return httpx.Response(404, json={})

    return httpx.MockTransport(handler)


def _client(session, token_responses, calls=None):
    calls = calls if calls is not None else []
    http = httpx.Client(transport=_transport(token_responses, calls))
    return BouncieClient(session, config=CONFIG, http=http), calls


def _ok(access="acc-1", refresh="ref-1", expires=3600):
    return 200, {
        "access_token": access,
        "refresh_token": refresh,
        "expires_in": expires,
        "token_type": "Bearer",
    }


def test_auth_code_exchange_stores_the_token(session):
    client, _ = _client(session, [_ok()])
    assert client.access_token() == "acc-1"
    row = session.scalar(select(OAuthToken))
    assert row.refresh_token == "ref-1"
    assert row.refresh_count == 0


def test_a_valid_token_is_reused_rather_than_re_exchanged(session):
    client, calls = _client(session, [_ok()])
    client.access_token()
    client.access_token()
    assert sum(1 for c in calls if c.url.path == "/oauth/token") == 1


def test_expired_token_refreshes_and_persists_the_rotated_pair(session):
    """Bouncie rotates refresh tokens and kills the old one, so the new pair
    must land in the database or the chain is lost."""
    client, calls = _client(session, [_ok(), _ok("acc-2", "ref-2")])
    client.access_token()
    row = session.scalar(select(OAuthToken))
    row.expires_at = datetime.now(UTC) - timedelta(minutes=1)
    session.commit()

    assert client.access_token() == "acc-2"
    session.refresh(row)
    assert row.refresh_token == "ref-2"
    assert row.refresh_count == 1
    grants = [c for c in calls if c.url.path == "/oauth/token"]
    assert json.loads(grants[-1].content)["grant_type"] == "refresh_token"


def test_a_dead_refresh_chain_self_heals_via_the_auth_code(session):
    """Refresh tokens expire if unused, and a crash mid-rotation can orphan the
    chain. The authorization code never expires, so recovery needs no human."""
    client, calls = _client(
        session, [_ok(), (400, {"error": "invalid_grant"}), _ok("acc-3", "ref-3")]
    )
    client.access_token()
    row = session.scalar(select(OAuthToken))
    row.expires_at = datetime.now(UTC) - timedelta(minutes=1)
    session.commit()

    assert client.access_token() == "acc-3"
    grants = [c for c in calls if c.url.path == "/oauth/token"]
    assert json.loads(grants[-1].content)["grant_type"] == "authorization_code"


def test_api_calls_send_a_raw_token_not_a_bearer_token(session):
    """The token response says token_type "Bearer", but the API rejects
    "Bearer <token>" — it wants the bare token. Easy to regress, so pinned."""
    client, calls = _client(session, [_ok()])
    client.vehicles()
    api_call = [c for c in calls if c.url.path == "/v1/vehicles"][0]
    assert api_call.headers["Authorization"] == "acc-1"
    assert not api_call.headers["Authorization"].startswith("Bearer")


def test_a_token_response_without_a_token_is_an_error(session):
    client, _ = _client(session, [(200, {"expires_in": 3600})])
    with pytest.raises(BouncieError):
        client.access_token()


def test_sync_does_not_invent_fleet_vehicles_by_default(session):
    """A device on the account is not proof the operator wants a fleet row."""
    client, _ = _client(session, [_ok()])
    result = sync_vehicles(session, client)
    assert result.created == 0
    assert session.scalar(select(func.count()).select_from(Vehicle)) == 0
    assert sorted(result.unmatched_imeis) == ["111111111111111", "222222222222222"]


def test_sync_seeds_and_derives_capability_flags(session):
    client, _ = _client(session, [_ok()])
    result = sync_vehicles(session, client, create_missing=True)
    assert result.created == 2

    jolene = session.scalar(select(Vehicle).where(Vehicle.bouncie_nickname == "Jolene"))
    assert (jolene.reports_fuel_level, jolene.reports_obd_odometer) == (True, True)

    sparse = session.scalar(select(Vehicle).where(Vehicle.bouncie_nickname == "Sparse"))
    assert (sparse.reports_fuel_level, sparse.reports_obd_odometer) == (False, False)

    # Bouncie does not know plates, and toll matching joins on plate. A seeded
    # row must be missing one rather than carry a plausible-looking fake, which
    # would silently match no tolls and read as a quiet month.
    assert jolene.plate is None


def test_sync_is_idempotent_for_an_unmoved_vehicle(session):
    """Polling a parked car repeatedly must not accumulate snapshot rows."""
    client, _ = _client(session, [_ok()])
    sync_vehicles(session, client, create_missing=True)
    first = session.scalar(select(func.count()).select_from(TelemetryEvent))
    sync_vehicles(session, client, create_missing=True)
    assert session.scalar(select(func.count()).select_from(TelemetryEvent)) == first


def test_sync_matches_an_existing_vehicle_by_vin_when_imei_is_unset(session):
    """A car added to the registry before its tracker arrives should adopt the
    device rather than become a duplicate row."""
    session.add(
        Vehicle(
            nickname="Van",
            make="Ford",
            model="Transit",
            year=2024,
            plate="HZV3305",
            vin="VINSPARSE00000002",
        )
    )
    session.commit()

    client, _ = _client(session, [_ok()])
    result = sync_vehicles(session, client)
    assert result.matched == 1

    van = session.scalar(select(Vehicle).where(Vehicle.nickname == "Van"))
    assert van.bouncie_imei == "222222222222222"
    assert session.scalar(select(func.count()).select_from(Vehicle)) == 1


# ---------------------------------------------------------------------------
# Re-authorisation
# ---------------------------------------------------------------------------


def test_a_changed_authorization_code_is_exchanged_rather_than_refreshed(session, monkeypatch):
    """Bouncie consent is per device. Authorising two cars and later fitting a
    third leaves the third outside the grant, and the fix is to re-authorise
    and supply the new code.

    The trap is that the stored refresh chain keeps working indefinitely, so
    without noticing the code changed the client would refresh the *old* grant
    forever: the new car stays invisible and the operator's re-authorisation
    looks like it silently failed.
    """
    session.add(
        OAuthToken(
            provider="bouncie",
            access_token="old-grant-token",
            refresh_token="old-refresh",
            expires_at=datetime.now(UTC) + timedelta(minutes=55),
            grant_fingerprint=hashlib.sha256(b"OLD-CODE").hexdigest(),
        )
    )
    session.commit()

    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body["grant_type"])
        return httpx.Response(
            200, json={"access_token": "new-grant-token", "refresh_token": "r", "expires_in": 3600}
        )

    cfg = BouncieConfig(
        client_id="cid", client_secret="sec", auth_code="NEW-CODE", redirect_uri="http://x/cb"
    )
    client = BouncieClient(
        session, cfg, http=httpx.Client(transport=httpx.MockTransport(handler))
    )

    assert client.access_token() == "new-grant-token"
    assert seen == ["authorization_code"], "the new code must be exchanged, not the old refreshed"

    row = session.scalar(select(OAuthToken).where(OAuthToken.provider == "bouncie"))
    assert row.grant_fingerprint == hashlib.sha256(b"NEW-CODE").hexdigest()


def test_an_unchanged_code_still_uses_the_stored_token(session):
    """The check must not make every call re-exchange; that would throw away a
    live token on every poll."""
    code = "SAME-CODE"
    session.add(
        OAuthToken(
            provider="bouncie",
            access_token="still-good",
            refresh_token="r",
            expires_at=datetime.now(UTC) + timedelta(minutes=55),
            grant_fingerprint=hashlib.sha256(code.encode()).hexdigest(),
        )
    )
    session.commit()

    def explode(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("re-exchanged a grant that had not changed")

    cfg = BouncieConfig(
        client_id="cid", client_secret="sec", auth_code=code, redirect_uri="http://x/cb"
    )
    client = BouncieClient(
        session, cfg, http=httpx.Client(transport=httpx.MockTransport(explode))
    )
    assert client.access_token() == "still-good"


def test_a_grant_stored_before_fingerprints_existed_is_re_exchanged_once(session):
    """The live row predates the column. NULL means "unknown authorization", so
    it is re-exchanged once and stamped rather than left ambiguous forever."""
    session.add(
        OAuthToken(
            provider="bouncie",
            access_token="legacy",
            refresh_token="r",
            expires_at=datetime.now(UTC) + timedelta(minutes=55),
            grant_fingerprint=None,
        )
    )
    session.commit()

    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content)["grant_type"])
        return httpx.Response(
            200, json={"access_token": "fresh", "refresh_token": "r2", "expires_in": 3600}
        )

    cfg = BouncieConfig(
        client_id="cid", client_secret="sec", auth_code="CODE", redirect_uri="http://x/cb"
    )
    http = httpx.Client(transport=httpx.MockTransport(handler))
    assert BouncieClient(session, cfg, http=http).access_token() == "fresh"
    # Stamped now, so a second call does not exchange again.
    assert BouncieClient(session, cfg, http=http).access_token() == "fresh"
    assert calls == ["authorization_code"]
