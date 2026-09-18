"""Turn telemetry into parking sessions.

Bouncie has no ignition event, so "parked" means "the last trip ended here".
A ``tripEnd`` opens a session at that point; a ``tripStart`` closes it.

The hard part is not the session, it is deciding which side of the street the
car is on. Alternate-side rules are side-specific, a Brooklyn side street is
about 11 m curb to curb, and the device's own fix is good to 5-10 m. Measured
against a real reported position, the two curbs came out 3.9 m and 7.2 m away —
a 3.3 m margin, well inside the error. So a nearest-side guess is a coin flip
and is never applied silently: it is offered for confirmation, and only a
confirmed side drives a deadline.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from turonomics_api.db.models import ParkingSession, StreetSegmentSide, Vehicle

# Generous on purpose. Measured against real reported positions, a parked car
# can sit 40 m from the nearest signed kerb: signs do not cover every block, and
# a fix taken between tall buildings drifts. A tight radius returns "no
# candidates", which reads as "no rules here" and therefore as "nothing due" —
# the silent failure this design exists to avoid. Better to offer several
# candidates with honest distances and let the operator pick.
SEARCH_RADIUS_M = 75.0

# Below this margin between the best and second-best candidate, the guess is
# not meaningfully better than a coin flip. Kept as a named constant because it
# is a claim about GPS accuracy, not a tuning knob.
AMBIGUOUS_MARGIN_M = 7.0


@dataclass(frozen=True)
class SideGuess:
    segment_side: StreetSegmentSide | None
    distance_m: float | None
    runner_up_m: float | None
    confidence: float

    @property
    def is_ambiguous(self) -> bool:
        """Whether the runner-up is close enough that the guess is unsafe."""
        if self.runner_up_m is None or self.distance_m is None:
            return False
        return (self.runner_up_m - self.distance_m) < AMBIGUOUS_MARGIN_M


def resolve_side(
    session: Session,
    *,
    lat: float,
    lon: float,
    needs_large_spot: bool = False,
    radius_m: float = SEARCH_RADIUS_M,
) -> SideGuess:
    """Best-guess segment side for a point, with an honest confidence.

    ``needs_large_spot`` filters out segments a van does not fit on. NYC has no
    length-based cleaning rule, so this is about whether the spot is usable at
    all, not about which rule applies.
    """
    point = func.ST_GeogFromText(f"SRID=4326;POINT({lon} {lat})")
    distance = func.ST_Distance(StreetSegmentSide.geom, point)

    query = (
        select(StreetSegmentSide, distance.label("distance_m"))
        .where(StreetSegmentSide.geom.is_not(None))
        .where(func.ST_DWithin(StreetSegmentSide.geom, point, radius_m))
        .order_by(distance)
        .limit(2)
    )
    if needs_large_spot:
        query = query.where(StreetSegmentSide.fits_van.is_not(False))

    rows = session.execute(query).all()
    if not rows:
        return SideGuess(None, None, None, 0.0)

    best, best_m = rows[0]
    runner_up_m = float(rows[1][1]) if len(rows) > 1 else None

    if runner_up_m is None:
        # Only one candidate in range. Still not certain — the other side may
        # simply be missing from the dataset — but there is nothing to confuse
        # it with, so this is as good as a guess gets.
        confidence = 0.75
    else:
        margin = runner_up_m - float(best_m)
        # Full confidence needs a margin comfortably beyond GPS error.
        confidence = max(0.0, min(1.0, margin / (AMBIGUOUS_MARGIN_M * 2)))

    return SideGuess(best, float(best_m), runner_up_m, round(confidence, 2))


def open_parking_session(
    session: Session,
    *,
    vehicle: Vehicle,
    lat: float,
    lon: float,
    at: datetime,
) -> ParkingSession:
    """Record that a vehicle came to rest here.

    Idempotent for a repeated ``tripEnd``: an open session at the same place is
    returned rather than duplicated, because webhook retries are routine.
    """
    existing = session.scalar(
        select(ParkingSession)
        .where(ParkingSession.vehicle_id == vehicle.id, ParkingSession.ended_at.is_(None))
        .order_by(ParkingSession.started_at.desc())
        .limit(1)
    )

    point = f"SRID=4326;POINT({lon} {lat})"

    if existing is not None:
        moved_m = session.scalar(
            select(func.ST_Distance(ParkingSession.location, func.ST_GeogFromText(point))).where(
                ParkingSession.id == existing.id
            )
        )
        # A few metres of GPS jitter on a stationary car is not a new spot.
        if moved_m is not None and moved_m < 15.0:
            return existing
        close_parking_session(session, vehicle=vehicle, at=at)

    guess = resolve_side(session, lat=lat, lon=lon, needs_large_spot=vehicle.needs_large_spot)
    record = ParkingSession(
        vehicle_id=vehicle.id,
        location=point,
        started_at=at,
        guessed_segment_side_id=guess.segment_side.id if guess.segment_side else None,
        guess_confidence=guess.confidence,
    )
    session.add(record)
    session.flush()
    return record


def close_parking_session(
    session: Session, *, vehicle: Vehicle, at: datetime
) -> ParkingSession | None:
    """The car has driven off; the parking clock no longer applies."""
    open_session = session.scalar(
        select(ParkingSession)
        .where(ParkingSession.vehicle_id == vehicle.id, ParkingSession.ended_at.is_(None))
        .order_by(ParkingSession.started_at.desc())
        .limit(1)
    )
    if open_session is None:
        return None
    open_session.ended_at = at
    open_session.must_move_by = None
    session.flush()
    return open_session


def confirm_side(
    session: Session,
    *,
    parking_session: ParkingSession,
    segment_side: StreetSegmentSide,
    confirmed_at: datetime,
    confirmed_by_id: uuid.UUID | None = None,
) -> ParkingSession:
    """Apply the operator's answer.

    A correction is recorded as such: it is the signal worth learning from, and
    the only evidence that the guess was wrong in a way worth improving.
    """
    parking_session.was_corrected = (
        parking_session.guessed_segment_side_id is not None
        and parking_session.guessed_segment_side_id != segment_side.id
    )
    parking_session.segment_side_id = segment_side.id
    parking_session.confirmed_at = confirmed_at
    parking_session.confirmed_by_id = confirmed_by_id
    session.flush()
    return parking_session


def apply_engine_state(
    session: Session,
    *,
    vehicle: Vehicle,
    is_running: bool | None,
    lat: float | None,
    lon: float | None,
    at: datetime,
) -> ParkingSession | None:
    """Open or close a parking session from Bouncie's engine state.

    Bouncie reports ``stats.isRunning`` on every poll, so a parked car is one
    whose engine is off — no need to wait for a ``tripEnd`` webhook or to infer
    stillness from consecutive fixes.

    ``is_running`` of ``None`` means the provider did not say, which is not the
    same as stopped. Nothing is opened or closed on silence: guessing "parked"
    from a missing field would start a deadline the operator never earned, and
    guessing "moving" would cancel one they need.
    """
    if is_running is None:
        return None

    if is_running:
        return close_parking_session(session, vehicle=vehicle, at=at)

    if lat is None or lon is None:
        # Stopped, but the provider sent no position. A session without a place
        # cannot produce a deadline, and inventing a place is worse than none.
        return None

    return open_parking_session(session, vehicle=vehicle, lat=lat, lon=lon, at=at)
