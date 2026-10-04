"""Let a browser subscribe to alerts, and prove the round trip works.

A push subscription is created by the browser, not by this server: the browser
asks its own push service for an endpoint, then hands the endpoint and two keys
here to be stored. So these endpoints are a key-value store with opinions, plus
a test button — because the only way to know push works end to end is to
receive one, and discovering that on the first real street-cleaning deadline is
too late.

These are unauthenticated, which is a deliberate and narrow choice: the app has
no login yet, and the worst a stranger can do with ``/subscribe`` is register
their own browser to receive this fleet's move reminders. Setting ``SYNC_TOKEN``
closes it; see ``require_token``.
"""

from __future__ import annotations

import hmac
import logging
import os
import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from turonomics_api.db.base import get_session
from turonomics_api.db.models import PushSubscription
from turonomics_api.notify import webpush
from turonomics_api.notify.alerts import Alert
from turonomics_api.settings import site_url

log = logging.getLogger("turonomics.routers.push")

router = APIRouter(prefix="/api/push", tags=["push"])

DbSession = Annotated[Session, Depends(get_session)]


class KeyResponse(BaseModel):
    configured: bool
    public_key: str | None = None


class SubscribeRequest(BaseModel):
    endpoint: str = Field(min_length=10, max_length=2000)
    # Named as the browser names them, so the client can post
    # ``subscription.toJSON()`` without reshaping it.
    keys: dict[str, str]
    label: str | None = Field(default=None, max_length=120)


class SubscribeResponse(BaseModel):
    id: uuid.UUID
    created: bool


class TestResponse(BaseModel):
    sent: int
    removed: int
    failed: int


def require_token(authorization: str | None) -> None:
    """Enforce ``SYNC_TOKEN`` if it is set, and allow everything if it is not.

    The opposite default from ``/api/sync``, and for a different reason: there,
    an open endpoint burns the Bouncie rate limit the fleet depends on. Here, a
    closed-by-default endpoint means the operator cannot subscribe their own
    phone without first inventing a token, and an alerting system nobody can
    turn on is worse than one a stranger could subscribe to.
    """
    expected = os.environ.get("PUSH_TOKEN", "").strip()
    if not expected:
        return
    supplied = (authorization or "").removeprefix("Bearer ").strip()
    if not supplied or not hmac.compare_digest(supplied, expected):
        raise HTTPException(401, "bad or missing token")


@router.get("/key", response_model=KeyResponse)
def key() -> KeyResponse:
    """The ``applicationServerKey`` to subscribe with, if push is configured.

    Reports ``configured: false`` rather than failing, so the page can show
    "alerts are not set up on the server" instead of a broken button.
    """
    if not webpush.configured():
        return KeyResponse(configured=False)
    return KeyResponse(configured=True, public_key=webpush.public_key())


@router.post("/subscribe", response_model=SubscribeResponse)
def subscribe(
    body: SubscribeRequest,
    session: DbSession,
    authorization: Annotated[str | None, Header()] = None,
    user_agent: Annotated[str | None, Header()] = None,
) -> SubscribeResponse:
    require_token(authorization)
    p256dh = body.keys.get("p256dh", "").strip()
    auth = body.keys.get("auth", "").strip()
    if not p256dh or not auth:
        # Both are required to encrypt. Storing a subscription without them
        # would accept the request and then fail silently at every send.
        raise HTTPException(422, "subscription is missing p256dh or auth")

    existing = session.scalar(
        select(PushSubscription).where(PushSubscription.endpoint == body.endpoint)
    )
    if existing is not None:
        # Re-subscribing is normal: a browser does it whenever its own keys
        # rotate, keeping the endpoint. Updating beats a unique-violation 500.
        existing.p256dh = p256dh
        existing.auth = auth
        existing.failures = 0
        if body.label or user_agent:
            existing.label = body.label or _label(user_agent)
        session.commit()
        return SubscribeResponse(id=existing.id, created=False)

    row = PushSubscription(
        endpoint=body.endpoint,
        p256dh=p256dh,
        auth=auth,
        label=body.label or _label(user_agent),
    )
    session.add(row)
    session.commit()
    log.info("new push subscription: %s", row.label or "unlabelled")
    return SubscribeResponse(id=row.id, created=True)


@router.post("/unsubscribe")
def unsubscribe(
    body: SubscribeRequest,
    session: DbSession,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, bool]:
    require_token(authorization)
    row = session.scalar(
        select(PushSubscription).where(PushSubscription.endpoint == body.endpoint)
    )
    if row is None:
        # Idempotent. The browser calls this after it has already discarded its
        # own subscription, so "it was not there" is success.
        return {"removed": False}
    session.delete(row)
    session.commit()
    return {"removed": True}


@router.post("/test", response_model=TestResponse)
def test(
    session: DbSession,
    authorization: Annotated[str | None, Header()] = None,
    now: datetime | None = None,
) -> TestResponse:
    """Send one notification to every subscription, outside the alert rules.

    Not recorded in ``notification``: this is a wire test, and giving it a
    dedupe key would either let it suppress a real alert or make it unrepeatable.
    """
    require_token(authorization)
    if not webpush.configured():
        raise HTTPException(503, "push is not configured; set VAPID_PRIVATE_KEY")
    stamp = (now or datetime.now(UTC)).strftime("%H:%M:%S")
    delivery = webpush.send(
        session,
        Alert(
            dedupe_key=f"test:{uuid.uuid4()}",
            title="Turonomics test",
            body=f"Alerts are working. Sent at {stamp} UTC.",
            url=site_url(),
        ),
    )
    session.commit()
    return TestResponse(sent=delivery.sent, removed=delivery.removed, failed=delivery.failed)


def _label(user_agent: str | None) -> str | None:
    """A short name for a device, from its User-Agent.

    Only enough to tell "the phone" from "the laptop" in a log. The full string
    is a fingerprint and is not worth storing to answer that question.
    """
    if not user_agent:
        return None
    for needle, name in (
        ("iPhone", "iPhone"),
        ("iPad", "iPad"),
        ("Android", "Android"),
        ("Macintosh", "Mac"),
        ("Windows", "Windows"),
        ("Linux", "Linux"),
    ):
        if needle in user_agent:
            return name
    return "unknown device"
