"""Send each alert once, over whatever channel is configured.

The poll runs every ten minutes and an approaching deadline stays approaching,
so "has this already been said" is the whole problem. ``Notification`` is the
record of what has been said; this module is the thing that consults it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from turonomics_api.db.models import Notification
from turonomics_api.notify import webpush
from turonomics_api.notify.alerts import Alert, alerts_due

log = logging.getLogger("turonomics.notify")

# The channel recorded when nothing can actually be delivered. Alerts still get
# decided and logged, so the rules can be checked against a real fleet before
# any key is set — which is the whole reason this is not an error.
LOG_ONLY = "log"


@dataclass
class DispatchResult:
    considered: int = 0
    sent: int = 0
    already_sent: int = 0
    retrying: int = 0
    subscriptions_dropped: int = 0

    def summary(self) -> str:
        return (
            f"{self.considered} due, {self.sent} sent, {self.already_sent} already sent, "
            f"{self.retrying} to retry, {self.subscriptions_dropped} subscription(s) dropped"
        )


def _already_sent(session: Session, alert: Alert) -> bool:
    return (
        session.scalar(select(Notification.id).where(Notification.dedupe_key == alert.dedupe_key))
        is not None
    )


def dispatch(
    session: Session, *, now: datetime | None = None, http: httpx.Client | None = None
) -> DispatchResult:
    """Decide, deliver, and remember. Never raises."""
    now = now or datetime.now(UTC)
    result = DispatchResult()
    try:
        due = alerts_due(session, now=now)
    except Exception as exc:  # noqa: BLE001 - a broken rule must not stop the poll
        log.warning("could not work out which alerts are due: %s", exc)
        return result

    result.considered = len(due)
    for alert in due:
        if _already_sent(session, alert):
            result.already_sent += 1
            continue

        if not webpush.configured():
            # Logged at info, in full. This is the state the app is in until
            # VAPID_PRIVATE_KEY is set, and a silent no-op would make "push is
            # not configured" indistinguishable from "nothing is due".
            log.info("alert (no channel configured): %s — %s", alert.title, alert.body)
            session.add(_record(alert, channel=LOG_ONLY, delivered=0, now=now))
            result.sent += 1
            continue

        delivery = webpush.send(session, alert, http=http, now=now)
        result.subscriptions_dropped += delivery.removed
        if delivery.sent == 0 and delivery.failed:
            # Every attempt failed, and none of them permanently. Not recorded,
            # so the next poll tries again — recording it here would discard
            # the alert over a push service having a bad minute.
            result.retrying += 1
            log.warning("alert not delivered, will retry: %s (%s)", alert.title, delivery.summary())
            continue
        session.add(_record(alert, channel=webpush.NAME, delivered=delivery.sent, now=now))
        result.sent += 1
        log.info("alert sent: %s — %s", alert.title, delivery.summary())

    # Logged unconditionally, even when nothing was due. A dispatcher that
    # reports nothing when it found nothing is indistinguishable from one that
    # is not running, and this project has already paid for that twice — once
    # in the poller, once in the mail sync. "No deadlines are close" is an
    # answer; silence is not.
    log.info("alerts: %s", result.summary())
    return result


def _record(alert: Alert, *, channel: str, delivered: int, now: datetime) -> Notification:
    return Notification(
        dedupe_key=alert.dedupe_key,
        channel=channel,
        title=alert.title,
        body=alert.body,
        task_id=alert.task_id,
        sent_at=now,
        delivered=delivered,
    )
