"""What Turo's own reservation detail says, as opposed to what its email said.

Email carries the trip times Turo wrote when it sent the notification. If a
guest extends a booking afterwards, no further email states the new end — so
the times in this database are the times at booking, and a crossing after the
original end looks like it belongs to nobody.

`/api/reservation/detail` carries the current booking. The extension fetches it
with the session the browser already holds (see docs/design/03-turo-api.md) and
posts it here. Two fields beyond the times are worth storing:

* ``allowedToRequestReimbursement`` — Turo answering whether an invoice can
  still be filed for this reservation, rather than ``TOLL_FILING_WINDOW_DAYS``,
  which is a number read off a help page.
* ``booking.gracePeriodEnd`` — stored and **not** used, because which grace
  period it is has not been established. It sits beside a cancellation policy
  block, and the policy endpoint next to it returns ``gracePeriodHours`` and
  ``leadTimeDays``, so it is quite likely the free-cancellation deadline
  measured from booking. The toll matcher's own two-hour grace is a guess, and
  replacing one guess with a field that might mean something else entirely
  would only make the guess harder to see. :func:`describe_grace_periods`
  prints where it actually falls, which settles it with one pull.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from turonomics_api.db.models import Trip, TripState

log = logging.getLogger("turonomics.ingest.turo_detail")

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _moment(value: Any) -> datetime | None:
    """A Turo timestamp block to an aware datetime.

    Turo sends ``{epochMillis, localDate, localTime}``. Only the first is
    unambiguous — ``localTime`` is "18:00" with the zone named in a sibling
    field — so the millis are what gets read.

    Added rather than divided, and the reason is weaker than it first looks:
    ``datetime.fromtimestamp(ms / 1000, UTC)`` gives the identical microsecond
    for every timestamp this app will ever see. The two diverge by 7µs around
    the year 9999 and not before, so a mutation between them is unkillable and
    there is no test here pretending otherwise. This form is kept because it
    is exact by construction rather than by the size of today's numbers.
    """
    if not isinstance(value, Mapping):
        return None
    millis = value.get("epochMillis")
    if not isinstance(millis, int) or isinstance(millis, bool):
        return None
    return _EPOCH + timedelta(milliseconds=millis)


@dataclass(frozen=True)
class ReservationDetail:
    """The parts of a reservation detail payload this app has a use for."""

    reservation_id: str
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    grace_period_ends_at: datetime | None = None
    can_file_reimbursement: bool | None = None
    license_plate: str | None = None


def parse_detail(payload: Mapping[str, Any]) -> ReservationDetail | None:
    """One ``/api/reservation/detail`` response, or None if it is not one.

    None rather than raising: the extension posts what it got, and a Turo error
    body or a changed route is a thing to report rather than a crash.
    """
    reservation = payload.get("id")
    if isinstance(reservation, bool) or not isinstance(reservation, int):
        return None
    booking = payload.get("booking")
    booking = booking if isinstance(booking, Mapping) else {}
    registration = booking.get("vehicleRegistration")
    registration = registration if isinstance(registration, Mapping) else {}
    plate = registration.get("licensePlate")
    allowed = payload.get("allowedToRequestReimbursement")
    return ReservationDetail(
        reservation_id=str(reservation),
        starts_at=_moment(booking.get("start")),
        ends_at=_moment(booking.get("end")),
        grace_period_ends_at=_moment(booking.get("gracePeriodEnd")),
        can_file_reimbursement=allowed if isinstance(allowed, bool) else None,
        license_plate=plate.strip().upper() if isinstance(plate, str) and plate.strip() else None,
    )


@dataclass
class DetailResult:
    seen: int = 0
    # Reservations this fleet has no trip for. Not an error: the account has
    # trips older than this database.
    unknown: list[str] = field(default_factory=list)
    stored: int = 0
    # Where Turo's booking disagrees with what the email said, which is the
    # entire reason for pulling this.
    retimed: list[str] = field(default_factory=list)
    # A plate that is not the one this app matched the rental to.
    wrong_plate: list[str] = field(default_factory=list)


def apply_detail(
    session: Session, detail: ReservationDetail, *, now: datetime, result: DetailResult
) -> Trip | None:
    """Store one reservation's detail against its trip.

    Turo's booking times win. That is the point of fetching them — the email
    states the times as they were when it was sent, and nothing re-states them
    when a guest extends. A changed end time moves which crossings fall inside
    the rental, so each change is reported rather than applied quietly, and the
    caller re-runs attribution.
    """
    result.seen += 1
    trip = session.scalar(select(Trip).where(Trip.turo_trip_id == detail.reservation_id))
    if trip is None:
        result.unknown.append(detail.reservation_id)
        return None

    if detail.license_plate and trip.vehicle is not None:
        known = (trip.vehicle.plate or "").replace(" ", "").upper()
        if known and known != detail.license_plate.replace(" ", ""):
            # Reported, not corrected. A rental on the wrong car means the
            # crossings on it are billed to the wrong guest, and reassigning a
            # trip between vehicles from inside a sync is not a repair, it is a
            # second guess.
            result.wrong_plate.append(f"{detail.reservation_id}: {trip.vehicle.nickname}")

    # Bound to locals so one condition does the narrowing as well as the
    # guarding: an end before its start is a payload to ignore, not a trip to
    # write, and the interval has a database check constraint besides.
    starts, ends = detail.starts_at, detail.ends_at
    if (
        starts is not None
        and ends is not None
        and ends > starts
        and (trip.starts_at, trip.ends_at) != (starts, ends)
    ):
        was = f"{trip.starts_at:%Y-%m-%d %H:%M}–{trip.ends_at:%H:%M}"
        now_is = f"{starts:%Y-%m-%d %H:%M}–{ends:%H:%M}"
        result.retimed.append(f"{detail.reservation_id}: {was} -> {now_is}")
        trip.starts_at = starts
        trip.ends_at = ends

    trip.grace_period_ends_at = detail.grace_period_ends_at
    trip.can_file_reimbursement = detail.can_file_reimbursement
    trip.detail_synced_at = now
    result.stored += 1
    return trip


def wanted_reservations(session: Session, *, limit: int = 200) -> list[str]:
    """Which reservations to fetch, never-fetched first.

    Ordered so that a pull interrupted halfway has still made progress on the
    rentals nothing is known about, rather than refreshing the same newest
    handful every time.
    """
    rows = session.scalars(
        select(Trip.turo_trip_id)
        .where(Trip.turo_trip_id.is_not(None), Trip.state != TripState.cancelled)
        .order_by(Trip.detail_synced_at.is_not(None), Trip.ends_at.desc())
        .limit(limit)
    ).all()
    return [row for row in rows if row]


def describe_grace_periods(session: Session) -> list[str]:
    """Where ``gracePeriodEnd`` actually falls, so it stops being a mystery.

    One line per rental, as an offset from the booking's start and end. If it
    lands a few hours after the start it is the free-cancellation deadline and
    is no use for attributing a late crossing. If it lands after the end, it is
    the return grace this app has been guessing at with a fixed two hours, and
    the matcher should use it.
    """
    lines: list[str] = []
    trips = session.scalars(
        select(Trip)
        .where(Trip.grace_period_ends_at.is_not(None))
        .order_by(Trip.ends_at.desc())
    ).all()
    for trip in trips:
        grace = trip.grace_period_ends_at
        if grace is None:  # pragma: no cover - the query says otherwise
            continue
        from_start = (grace - trip.starts_at).total_seconds() / 3600
        from_end = (grace - trip.ends_at).total_seconds() / 3600
        lines.append(
            f"{trip.turo_trip_id}: grace {from_start:+.1f}h from start, "
            f"{from_end:+.1f}h from end"
        )
    return lines
