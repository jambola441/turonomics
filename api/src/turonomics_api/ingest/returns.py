"""When a car actually came back, from the tracker rather than from a guess.

Turo's trip end is when the guest marked the car returned, which is not when
they stopped driving it. A guest who runs over without extending the booking
leaves their last crossings outside every rental window, and they were the only
person with the keys — so `ingest.tolls` allows a fixed grace after the end
(`TOLL_OVERRUN_GRACE_MINUTES`, two hours) and attributes crossings inside it.

Two hours is a guess in both directions. It bills the operator's own 11pm
errand to a guest whose car was home by nine, and it eats a 3am crossing by a
guest who got back at four. Turo publishes nothing that would settle it:
`booking.gracePeriodEnd` turned out to be the free-cancellation deadline, which
falls *before* the trip starts on every reservation in this fleet.

The cars have trackers, though, and Bouncie reports engine state on every poll.
A parked car is one whose engine is off, and `ParkingSession` already records
each time one comes to rest. So the moment a rental's car came back is the
moment it came to rest and *stayed* at rest — and a crossing between the
booking's end and that moment is the guest's, on evidence rather than on a
window.

What this does not do is extend a rental indefinitely. A car that is driven
all evening because the operator took it out has no quiet moment, and the
fixed grace stays as a ceiling — the tracker can narrow the window or confirm
it, never widen it past what the guess already allowed. Billing a guest for
somebody else's driving is the expensive mistake here, and the tracker cannot
tell who is holding the keys.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from turonomics_api.db.models import ParkingSession

log = logging.getLogger("turonomics.ingest.returns")

# How long a car has to stay off before it counts as having come back, rather
# than having stopped for coffee. Bouncie polls often enough that a short
# errand shows up as its own session, and treating one as the return would end
# a rental in the middle of it.
SETTLED_FOR = timedelta(minutes=30)


@dataclass(frozen=True)
class Return:
    """When a car came to rest, and how that was decided."""

    at: datetime
    # True when a parking session said so, False when the fixed grace did.
    from_tracker: bool


def settled_at(
    session: Session,
    vehicle_id: uuid.UUID,
    *,
    ends_at: datetime,
    grace: timedelta,
    now: datetime,
) -> Return:
    """When this vehicle came back, between its booking's end and the grace.

    Looks for the first parking session starting at or after ``ends_at`` and
    inside the grace, which then lasted ``SETTLED_FOR`` — or is still open,
    because a car nobody has moved since is plainly back.

    Falls back to ``ends_at + grace`` when the tracker has nothing to say,
    which is the behaviour this replaces. A fleet without trackers, a car whose
    OBD dongle was unplugged, and a statement imported before the telemetry
    arrives all land there, and all of them used to be the only case.
    """
    ceiling = ends_at + grace
    candidates = session.scalars(
        select(ParkingSession)
        .where(
            ParkingSession.vehicle_id == vehicle_id,
            ParkingSession.started_at >= ends_at,
            ParkingSession.started_at <= ceiling,
        )
        .order_by(ParkingSession.started_at)
    ).all()

    for parked in candidates:
        if parked.ended_at is None:
            # Still parked. Either it came back and nobody has moved it, or the
            # tracker has gone quiet — and a session that has been open longer
            # than the grace is not evidence of anything recent, so it only
            # counts while `now` is still inside the window it would set.
            if now - parked.started_at >= SETTLED_FOR:
                return Return(at=parked.started_at, from_tracker=True)
            continue
        if parked.ended_at - parked.started_at >= SETTLED_FOR:
            return Return(at=parked.started_at, from_tracker=True)

    return Return(at=ceiling, from_tracker=False)
