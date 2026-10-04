"""Keep the fleet view current between deploys.

Until this existed the only thing that ever called Bouncie was the boot
bootstrap, so the run sheet froze at whatever the last restart captured. A car
that parked an hour after a deploy stayed invisible until the next one.

The poll itself is just ``sync_vehicles`` on a timer; the work of turning
engine state into a parking clock lives there, and runs on every poll rather
than only when the telemetry log grows. See the comment in that module for why
that distinction is load-bearing.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from turonomics_api.bouncie.client import BouncieClient
from turonomics_api.bouncie.sync import SyncResult, sync_vehicles
from turonomics_api.ingest.mail import sync_trips_from_mail

log = logging.getLogger("turonomics.poller")

DEFAULT_INTERVAL_MINUTES = 10


def poll_once(session: Session, *, client: BouncieClient | None = None) -> SyncResult:
    """One cycle: pull current state from Bouncie and re-derive parking from it.

    ``create_missing`` is on here. The operator owns the Bouncie account, so
    fitting a device to a car *is* how they add it to the fleet — and with it
    off the poll collected the new device into ``unmatched_imeis`` and dropped
    it, leaving the car invisible until the next deploy with nothing saying
    why. The cost of being wrong is a row that needs renaming; the cost of the
    old behaviour was a car nobody could see.
    """
    result = sync_vehicles(session, client or BouncieClient(session), create_missing=True)
    if result.created:
        log.info("adopted %d new device(s) from the Bouncie account", result.created)
    # Trips in the same cycle as positions. Reading them on a separate timer
    # would let the run sheet show a car as free while the mail saying a guest
    # has it waits for a different clock.
    sync_trips_from_mail(session, now=datetime.now(UTC))
    session.commit()
    return result


def summarize(result: SyncResult) -> str:
    return f"{result.events} new events, {result.parked} parked"


def interval_minutes() -> int:
    """Minutes between polls. ``0`` switches the loop off.

    Defaults to on. An app whose job is to tell you when to move a car before
    it is ticketed should not need a flag set before it starts watching.
    """
    raw = os.environ.get("SYNC_INTERVAL_MINUTES", "").strip()
    if not raw:
        return DEFAULT_INTERVAL_MINUTES
    try:
        return max(0, int(raw))
    except ValueError:
        log.warning("SYNC_INTERVAL_MINUTES=%r is not a number — using the default", raw)
        return DEFAULT_INTERVAL_MINUTES
