"""Keep the fleet view current between deploys.

Until this existed the only thing that ever called Bouncie was the boot
bootstrap, so the run sheet froze at whatever the last restart captured. A car
that parked an hour after a deploy stayed invisible until the next one.

Two halves, and the second is the one that is easy to miss:

``sync_vehicles`` records new telemetry and drives the parking clock off it,
but only for events it has not seen before — which is right, since a webhook
retry must not open a second session. The consequence is that a fix recorded
*before* the parking logic shipped never passes through it, and a car sitting
on one of those fixes has no session and no deadline for as long as it stays
still. Reconciling replays each vehicle's latest known fix through the same
logic every poll, which is safe because opening a session is idempotent within
15m and closing one the car has already left is a no-op.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from turonomics_api.bouncie.client import BouncieClient
from turonomics_api.bouncie.sync import sync_vehicles
from turonomics_api.db.models import Vehicle
from turonomics_api.ingest.parking import apply_engine_state
from turonomics_api.ingest.tasks import refresh_move_task

log = logging.getLogger("turonomics.poller")

DEFAULT_INTERVAL_MINUTES = 10


@dataclass
class PollResult:
    events: int = 0
    reconciled: int = 0

    def summary(self) -> str:
        return f"{self.events} new events, {self.reconciled} vehicles reconciled"


def reconcile_engine_state(session: Session) -> int:
    """Re-derive parking from each vehicle's latest known fix.

    Returns the number of vehicles that produced or kept a parking session.
    """
    touched = 0
    for vehicle in session.scalars(select(Vehicle)).all():
        # Raw SQL for the same reason as the fleet router: the cast to geometry
        # is what exposes ST_X/ST_Y on a geography column.
        latest = session.execute(
            text(
                "SELECT is_running, occurred_at, "
                "       ST_Y(location::geometry) AS lat, ST_X(location::geometry) AS lon "
                "FROM telemetry_event "
                "WHERE vehicle_id = :vid AND is_running IS NOT NULL "
                "ORDER BY occurred_at DESC LIMIT 1"
            ),
            {"vid": str(vehicle.id)},
        ).first()
        if latest is None:
            continue

        session_row = apply_engine_state(
            session,
            vehicle=vehicle,
            is_running=latest.is_running,
            lat=latest.lat,
            lon=latest.lon,
            at=latest.occurred_at,
        )
        refresh_move_task(session, vehicle=vehicle)
        if session_row is not None:
            touched += 1
    return touched


def poll_once(session: Session, *, client: BouncieClient | None = None) -> PollResult:
    """One cycle: pull fresh telemetry, then re-derive parking from what we hold."""
    result = PollResult()
    result.events = sync_vehicles(session, client or BouncieClient(session)).events
    result.reconciled = reconcile_engine_state(session)
    session.commit()
    return result


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
