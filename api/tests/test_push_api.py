"""Storing a browser's subscription, and the ways that goes wrong in practice."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from turonomics_api.db.models import PushSubscription
from turonomics_api.notify.vapid import generate_private_key, load_private_key, public_key_of

from .conftest import requires_db

pytestmark = requires_db

UA_PUBLIC = "BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4"
AUTH_SECRET = "BTBZMqHH6r4Tts7J_aSIgg"
ENDPOINT = "https://push.example.net/s/abc123"


def _body(endpoint: str = ENDPOINT) -> dict:
    # Shaped like PushSubscription.toJSON() in a browser, extra field included:
    # the client posts it verbatim rather than reshaping it.
    return {
        "endpoint": endpoint,
        "expirationTime": None,
        "keys": {"p256dh": UA_PUBLIC, "auth": AUTH_SECRET},
    }


@pytest.fixture()
def configured(monkeypatch):
    monkeypatch.setenv("VAPID_PRIVATE_KEY", generate_private_key())


def test_the_key_endpoint_says_so_when_push_is_not_set_up(api_client, monkeypatch):
    """Reported rather than raised, so the page can say "not set up on the
    server" instead of showing a button that fails when pressed."""
    monkeypatch.delenv("VAPID_PRIVATE_KEY", raising=False)
    body = api_client.get("/api/push/key").json()
    assert body == {"configured": False, "public_key": None}


def test_the_key_endpoint_serves_the_public_half(api_client, monkeypatch):
    private = generate_private_key()
    monkeypatch.setenv("VAPID_PRIVATE_KEY", private)
    body = api_client.get("/api/push/key").json()
    assert body["configured"] is True
    assert body["public_key"] == public_key_of(load_private_key(private))


def test_subscribing_stores_the_endpoint_and_both_keys(api_client, session):
    response = api_client.post("/api/push/subscribe", json=_body())
    assert response.status_code == 200
    assert response.json()["created"] is True
    row = session.scalars(select(PushSubscription)).one()
    assert (row.endpoint, row.p256dh, row.auth) == (ENDPOINT, UA_PUBLIC, AUTH_SECRET)


def test_subscribing_twice_updates_rather_than_failing(api_client, session):
    """A browser re-subscribes whenever its own keys rotate, keeping the
    endpoint. Inserting blindly turns that into a unique-violation 500."""
    api_client.post("/api/push/subscribe", json=_body())
    rotated = _body()
    rotated["keys"]["auth"] = "Zm9vYmFyYmF6cXV1eHg"
    second = api_client.post("/api/push/subscribe", json=rotated)
    assert second.status_code == 200
    assert second.json()["created"] is False
    row = session.scalars(select(PushSubscription)).one()
    assert row.auth == "Zm9vYmFyYmF6cXV1eHg"


def test_a_subscription_without_keys_is_refused(api_client, session):
    """Storing it would accept the request and then fail silently at every
    send, which is the worst available outcome."""
    body = _body()
    body["keys"] = {}
    assert api_client.post("/api/push/subscribe", json=body).status_code == 422
    assert session.scalars(select(PushSubscription)).all() == []


def test_the_device_is_labelled_from_its_user_agent_not_stored_whole(api_client, session):
    """Enough to tell the phone from the laptop in a log. The full string is a
    fingerprint and is not worth keeping to answer that question."""
    api_client.post(
        "/api/push/subscribe",
        json=_body(),
        headers={"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X)"},
    )
    assert session.scalars(select(PushSubscription)).one().label == "iPhone"


def test_unsubscribing_removes_it(api_client, session):
    api_client.post("/api/push/subscribe", json=_body())
    assert api_client.post("/api/push/unsubscribe", json=_body()).json() == {"removed": True}
    assert session.scalars(select(PushSubscription)).all() == []


def test_unsubscribing_something_already_gone_is_success(api_client):
    """The browser calls this after discarding its own subscription, so "it was
    not there" is the expected case, not an error."""
    assert api_client.post("/api/push/unsubscribe", json=_body()).json() == {"removed": False}


def test_the_test_endpoint_refuses_when_push_is_not_configured(api_client, monkeypatch):
    monkeypatch.delenv("VAPID_PRIVATE_KEY", raising=False)
    assert api_client.post("/api/push/test").status_code == 503


def test_a_token_closes_the_endpoints_when_one_is_set(api_client, monkeypatch, session):
    """Open by default on purpose — an alerting system the operator cannot
    switch on is worse than one a stranger could subscribe to — but closable."""
    monkeypatch.setenv("PUSH_TOKEN", "s3cret")
    assert api_client.post("/api/push/subscribe", json=_body()).status_code == 401
    allowed = api_client.post(
        "/api/push/subscribe", json=_body(), headers={"Authorization": "Bearer s3cret"}
    )
    assert allowed.status_code == 200
    assert len(session.scalars(select(PushSubscription)).all()) == 1
