"""Every reservation on the account, from Turo rather than from the mail.

Trips used to enter this database one way: a Turo email about them. The mail
sync reads a week back, and one backfill reached July — so the account's
earlier rentals were never here, and the Turo pull could not add them, because
it only ever asked Turo about reservations this database already had.

Turo's pages list the reservations themselves. The extension fetches those
lists with the session the browser holds and posts the bodies here; this finds
the reservations in them, adds any this database does not have, and leaves the
rest to the detail pull, which then fills in everything else.

Two lists, both what Turo's own Trips page fetches:

* ``/api/v2/feeds/trip-history`` — past trips, paged, grouped by month:
  ``tripHistoryFeeds: {list: [{month, trips: [reservation, ...]}], numPages}``.
  Every reservation, whether or not anyone messaged about it — unlike the
  inbox's conversation feed, which is a list of threads.
* ``/api/v2/feeds/upcoming-trips`` — ``upcomingTripItems``, a pickup and a
  return per upcoming rental, each naming its ``reservationId``.

Deliberately lenient about where in the body the reservations sit. A
reservation is recognised by its shape — an integer id and a start and end —
wherever it is.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from turonomics_api.db.models import Trip, TripSource, TripState, Vehicle
from turonomics_api.plates import normalize_plate

log = logging.getLogger("turonomics.ingest.turo_reservations")

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

# The interval pairs a reservation carries, in order of authority. `booking` is
# the current booking; the others are what was requested, or a span Turo uses
# for display, and only stand in when it is missing.
_SPANS = ("booking", "interval", "request")


def _moment(value: Any) -> datetime | None:
    if not isinstance(value, Mapping):
        return None
    millis = value.get("epochMillis")
    if not isinstance(millis, int) or isinstance(millis, bool):
        return None
    return _EPOCH + timedelta(milliseconds=millis)


def _span(entry: Mapping[str, Any]) -> tuple[datetime, datetime] | None:
    for key in _SPANS:
        span = entry.get(key)
        if isinstance(span, Mapping):
            start, end = _moment(span.get("start")), _moment(span.get("end"))
            if start is not None and end is not None and end > start:
                return start, end
    start, end = _moment(entry.get("tripStart")), _moment(entry.get("tripEnd"))
    if start is not None and end is not None and end > start:
        return start, end
    return None


def _reservation_id(value: Mapping[str, Any]) -> int | None:
    """Its id: `id` on a reservation, `reservationId` on an upcoming-trip item.

    The upcoming-trips feed lists events — a pickup and a return per rental —
    each naming its reservation, so the same id arrives twice and is
    de-duplicated by the caller.
    """
    for key in ("reservationId", "id"):
        found = value.get(key)
        if isinstance(found, int) and not isinstance(found, bool):
            return found
    return None


def _looks_like_one(value: Any) -> bool:
    return (
        isinstance(value, Mapping)
        and _reservation_id(value) is not None
        and _span(value) is not None
    )


def find_reservations(body: Any, *, depth: int = 0) -> Iterator[Mapping[str, Any]]:
    """Every reservation-shaped object in a Turo list body, outermost first.

    Stops descending into a reservation once found: its own nested objects
    (the vehicle, the renter) have integer ids too, and none has a span.
    """
    # Trip history nests each trip five deep (tripHistoryFeeds → list → month
    # → trips → trip); a cap of four found none of them.
    if depth > 8:
        return
    if _looks_like_one(body):
        yield body
        return
    if isinstance(body, Mapping):
        for value in body.values():
            yield from find_reservations(value, depth=depth + 1)
    elif isinstance(body, list):
        for value in body:
            yield from find_reservations(value, depth=depth + 1)


@dataclass(frozen=True)
class TuroReservation:
    reservation_id: str
    starts_at: datetime
    ends_at: datetime
    status: str | None
    guest_name: str | None
    plate: str | None
    vin: str | None
    listing_id: str | None

    @property
    def cancelled(self) -> bool:
        # By the status alone. The conversation feed carries a
        # `cancelledRequest` span on reservations that are plainly booked, so
        # its presence says nothing.
        return bool(self.status) and "CANCEL" in (self.status or "").upper()


def parse_reservation(entry: Mapping[str, Any]) -> TuroReservation | None:
    span = _span(entry)
    reservation = _reservation_id(entry)
    if span is None or reservation is None:
        return None
    vehicle = entry.get("vehicle")
    vehicle = vehicle if isinstance(vehicle, Mapping) else {}
    registration = vehicle.get("registration")
    registration = registration if isinstance(registration, Mapping) else {}
    plate = registration.get("licensePlate") or vehicle.get("licensePlate")
    vin = vehicle.get("vin")
    listing = vehicle.get("id")
    # The guest: `renter` on a reservation, `actor` on an upcoming-trip item.
    renter = entry.get("renter") or entry.get("actor")
    renter = renter if isinstance(renter, Mapping) else {}
    name = renter.get("firstName") or renter.get("name")
    status = entry.get("statusCode") or entry.get("status")
    return TuroReservation(
        reservation_id=str(reservation),
        starts_at=span[0],
        ends_at=span[1],
        status=status if isinstance(status, str) else None,
        guest_name=name.strip() if isinstance(name, str) and name.strip() else None,
        plate=normalize_plate(plate) if isinstance(plate, str) and plate.strip() else None,
        vin=vin.strip().upper() if isinstance(vin, str) and vin.strip() else None,
        listing_id=str(listing) if isinstance(listing, int) and not isinstance(listing, bool)
        else None,
    )


@dataclass
class ReservationsResult:
    found: int = 0
    known: int = 0
    created: list[str] = field(default_factory=list)
    # Reservations on a car this fleet cannot place. Reported with the plate,
    # because a trip has to belong to a car and guessing which is the mistake
    # the email matcher was written to avoid.
    unmatched: list[str] = field(default_factory=list)
    unreadable: int = 0


def _vehicle(session: Session, reservation: TuroReservation) -> Vehicle | None:
    """The fleet car, by Turo's listing id, then plate, then VIN.

    All three are identifiers rather than descriptions, so a match is a lookup.
    The listing id is learned from the mail; the plate and VIN are on file.
    """
    if reservation.listing_id:
        found = session.scalar(
            select(Vehicle).where(Vehicle.turo_listing_id == reservation.listing_id)
        )
        if found is not None:
            return found
    if reservation.plate:
        found = session.scalar(
            select(Vehicle).where(
                func.replace(func.replace(func.upper(Vehicle.plate), " ", ""), "-", "")
                == reservation.plate
            )
        )
        if found is not None:
            return found
    if reservation.vin:
        return session.scalar(select(Vehicle).where(func.upper(Vehicle.vin) == reservation.vin))
    return None


def apply_reservations(
    session: Session, body: Any, *, now: datetime, result: ReservationsResult
) -> list[str]:
    """Add the reservations in one list body that this database lacks.

    Returns the reservation ids the body held, in order, so the caller can
    tell a page it has seen before from a new one.

    A reservation already here is left alone: the detail pull is what keeps
    its times current, from a payload that says more than any list does.
    """
    ids: list[str] = []
    seen: set[str] = set()
    for entry in find_reservations(body):
        reservation = parse_reservation(entry)
        if reservation is None:
            result.unreadable += 1
            continue
        if reservation.reservation_id in seen:
            continue
        seen.add(reservation.reservation_id)
        ids.append(reservation.reservation_id)
        result.found += 1
        existing = session.scalar(
            select(Trip.id).where(Trip.turo_trip_id == reservation.reservation_id)
        )
        if existing is not None:
            result.known += 1
            continue
        vehicle = _vehicle(session, reservation)
        if vehicle is None:
            result.unmatched.append(
                f"{reservation.reservation_id}: {reservation.plate or 'no plate'}"
            )
            continue
        if reservation.cancelled:
            state = TripState.cancelled
        elif reservation.ends_at <= now:
            state = TripState.completed
        elif reservation.starts_at <= now:
            state = TripState.active
        else:
            state = TripState.upcoming
        session.add(
            Trip(
                vehicle_id=vehicle.id,
                turo_trip_id=reservation.reservation_id,
                guest_name=reservation.guest_name,
                starts_at=reservation.starts_at,
                ends_at=reservation.ends_at,
                state=state,
                # Found by the extension, as opposed to read from a Turo email.
                source=TripSource.extension,
                synced_at=now,
            )
        )
        session.flush()
        result.created.append(reservation.reservation_id)
        log.info(
            "turo list: added reservation %s (%s, %s)",
            reservation.reservation_id,
            vehicle.nickname,
            state,
        )
    return ids
