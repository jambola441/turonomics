"""Deliver an alert to every browser that subscribed.

A push service is a relay with opinions: it accepts an encrypted blob for one
subscription, holds it for the TTL, and tells you — via the status code — when
a subscription is gone for good. Acting on that last part is what keeps this
from accumulating dead endpoints and reporting failures forever.

Nothing here knows what an alert *means*; see ``notify.alerts`` for that.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from turonomics_api.db.models import PushSubscription
from turonomics_api.notify.alerts import Alert
from turonomics_api.notify.ece import b64url_decode, encrypt
from turonomics_api.notify.vapid import authorization, load_private_key, public_key_of
from turonomics_api.settings import vapid_private_key, vapid_subject

log = logging.getLogger("turonomics.notify.webpush")

NAME = "webpush"

# How long a push service should hold the message for a device that is offline.
# Three hours, because every alert here is about a deadline: a move warning
# delivered the next morning is worse than not delivered, since it reads as
# current.
TTL_SECONDS = 3 * 60 * 60

# One record of 4096 bytes, so the JSON has to fit in a little under 4KB once
# the delimiter and the GCM tag are counted. Titles and bodies here are a line
# each; this is a guard, not a budget.
MAX_PAYLOAD_BYTES = 3900

# What a push service says when a subscription will never work again: the
# browser cleared it, or the user revoked permission. Anything else is treated
# as transient.
GONE = (404, 410)


class NotConfigured(RuntimeError):
    """No VAPID key, so there is nothing to sign with."""


@dataclass
class Delivery:
    """What happened when one alert went out."""

    sent: int = 0
    removed: int = 0
    failed: int = 0

    def summary(self) -> str:
        return f"{self.sent} sent, {self.removed} removed, {self.failed} failed"


def configured() -> bool:
    return vapid_private_key() is not None


def public_key() -> str:
    """The ``applicationServerKey`` the browser must subscribe with."""
    encoded = vapid_private_key()
    if encoded is None:
        raise NotConfigured("VAPID_PRIVATE_KEY is not set")
    return public_key_of(load_private_key(encoded))


def payload_for(alert: Alert) -> bytes:
    body = json.dumps(
        {
            "title": alert.title,
            "body": alert.body,
            "url": alert.url,
            # The tag collapses a replaced notification rather than stacking a
            # second one. Keyed on the task, not the stage, so the one-hour
            # warning replaces the twelve-hour one on the lock screen.
            "tag": alert.dedupe_key.rsplit(":", 1)[0],
            "urgent": alert.urgent,
        },
        separators=(",", ":"),
    ).encode()
    if len(body) > MAX_PAYLOAD_BYTES:
        raise ValueError(f"payload is {len(body)} bytes, over the single-record limit")
    return body


def send(
    session: Session, alert: Alert, *, http: httpx.Client | None = None, now: datetime | None = None
) -> Delivery:
    """Post one alert to every live subscription. Never raises on a bad endpoint."""
    encoded = vapid_private_key()
    if encoded is None:
        raise NotConfigured("VAPID_PRIVATE_KEY is not set")
    private = load_private_key(encoded)
    subject = vapid_subject()
    payload = payload_for(alert)
    now = now or datetime.now(UTC)
    client = http or httpx.Client(timeout=15.0)
    result = Delivery()

    for subscription in session.scalars(select(PushSubscription)).all():
        try:
            body = encrypt(
                payload,
                ua_public=b64url_decode(subscription.p256dh),
                auth_secret=b64url_decode(subscription.auth),
            )
            response = client.post(
                subscription.endpoint,
                content=body,
                headers={
                    "Content-Encoding": "aes128gcm",
                    "Content-Type": "application/octet-stream",
                    "TTL": str(TTL_SECONDS),
                    "Urgency": "high" if alert.urgent else "normal",
                    "Authorization": authorization(
                        subscription.endpoint, private=private, subject=subject
                    ),
                },
            )
        except (httpx.HTTPError, ValueError) as exc:
            # A malformed stored key raises ValueError out of the curve code.
            # One bad subscription must not stop the others; this is the only
            # path by which the operator's phone gets told anything.
            result.failed += 1
            subscription.failures += 1
            log.warning("push to %s failed: %s", _short(subscription.endpoint), exc)
            continue

        if response.status_code in GONE:
            # Deleted rather than counted. A 410 is permanent, and a row that
            # can never succeed would otherwise report a failure every poll
            # forever and bury the real ones.
            log.info(
                "dropping a dead subscription (%s): %s",
                response.status_code,
                _short(subscription.endpoint),
            )
            session.delete(subscription)
            result.removed += 1
        elif 200 <= response.status_code < 300:
            subscription.delivered_at = now
            subscription.failures = 0
            result.sent += 1
        else:
            result.failed += 1
            subscription.failures += 1
            log.warning(
                "push to %s returned %d: %s",
                _short(subscription.endpoint),
                response.status_code,
                response.text[:200],
            )

    if http is None:
        client.close()
    return result


def _short(endpoint: str) -> str:
    """Enough of an endpoint to tell two apart, without logging the whole token.

    The endpoint is a bearer capability: anyone holding it can push to that
    browser until it expires. It does not belong in a retained log in full.
    """
    head, _, tail = endpoint.rpartition("/")
    host = head.split("//")[-1].split("/")[0]
    return f"{host}/…{tail[-6:]}"
