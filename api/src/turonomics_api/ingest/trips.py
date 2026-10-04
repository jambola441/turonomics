"""Turn parsed Turo emails into trip records.

Keyed on the reservation id, because that is the one identifier every email in
a trip's life carries. A booking creates the trip; a change, a reminder or a
message updates it; a cancellation closes it. All four are the same upsert.

Vehicle matching is the awkward part and is deliberately conservative. Turo
names the car as free text — "Ford Transit 2024" — with no plate and no VIN, so
a match is a guess. A trip attached to the wrong car would suppress street
cleaning on a vehicle that is actually parked and needs moving, which is worse
than a trip attached to no car at all. So an ambiguous match is left unmatched
and reported rather than resolved.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from turonomics_api.db.models import Trip, TripSource, TripState, Vehicle
from turonomics_api.gmail.parse import ParsedTrip

log = logging.getLogger("turonomics.ingest.trips")


@dataclass
class TripSyncResult:
    created: int = 0
    updated: int = 0
    unmatched: int = 0

    def summary(self) -> str:
        return (
            f"{self.created} created, {self.updated} updated, "
            f"{self.unmatched} with no vehicle matched"
        )


def _tokens(text: str) -> set[str]:
    return {word.lower() for word in text.replace("-", " ").split() if len(word) > 1}


def match_vehicle(
    session: Session, vehicle_text: str | None, *, listing_id: str | None = None
) -> Vehicle | None:
    """Find the fleet vehicle a Turo email refers to.

    Two routes, in order of how much they can be trusted:

    1. **The listing id**, read from the link behind the car's photo. Turo's own
       identifier for the car, so this is a lookup rather than a guess.
    2. **The vehicle text**, scored on shared words. Turo says "Ford Transit
       2024" where the registry holds make "Ford", model "Transit", year 2024,
       and the subject line sometimes says only "Transit".

    Returns None when nothing matches *or* when two vehicles tie on words. A tie
    means two cars of the same make and model, which this fleet has — two
    Corollas — and picking either would be a coin flip with a street-cleaning
    alert riding on it. Before the listing id existed that dropped half the
    mailbox; it is still the right answer when the id is absent.

    An unambiguous word match *teaches* the id, so a car with a distinctive
    model binds itself on the first email and never needs the fuzzy path again.
    """
    if listing_id:
        known = session.scalar(
            select(Vehicle).where(Vehicle.turo_listing_id == listing_id)
        )
        if known is not None:
            return known

    if not vehicle_text:
        if listing_id:
            log.info(
                "trip links Turo listing %s, which no fleet vehicle claims — "
                "bind it with `cli set <vehicle> --turo-listing %s`",
                listing_id,
                listing_id,
            )
        return None
    wanted = _tokens(vehicle_text)
    if not wanted:
        return None

    scored: list[tuple[int, Vehicle]] = []
    for vehicle in session.scalars(select(Vehicle)).all():
        haystack = _tokens(
            " ".join(
                part
                for part in (
                    vehicle.make,
                    vehicle.model,
                    str(vehicle.year or ""),
                    vehicle.nickname,
                    vehicle.bouncie_nickname or "",
                )
                if part
            )
        )
        overlap = len(wanted & haystack)
        if overlap:
            scored.append((overlap, vehicle))

    if not scored:
        return None
    scored.sort(key=lambda pair: -pair[0])
    best = scored[0][0]
    if sum(1 for score, _ in scored if score == best) > 1:
        # The log names the listing id when there is one, because that is the
        # single fact that resolves this permanently. Unmasked deliberately:
        # it is the operator's own listing, visible in the URL of their own
        # Turo page, and a log that says "ambiguous" without saying which
        # listing is a log you cannot act on.
        if listing_id:
            log.info(
                "vehicle %r matches more than one car equally well — Turo listing %s "
                "is unclaimed; bind it with `cli set <vehicle> --turo-listing %s`",
                vehicle_text,
                listing_id,
                listing_id,
            )
        else:
            log.info(
                "vehicle %r matches more than one car equally well and the email "
                "carried no car link — leaving unmatched",
                vehicle_text,
            )
        return None

    matched = scored[0][1]
    if listing_id and matched.turo_listing_id is None:
        # Learned, not assumed: reached only when the words picked out exactly
        # one car.
        #
        # No "is this listing already taken" check here, because it could never
        # fire: the lookup at the top of this function returns early for any
        # listing a vehicle already claims, so by this line nothing holds it.
        # A first draft had that guard, with a comment explaining what it
        # protected against; mutation testing showed it was unreachable. Dead
        # code that claims to be a safety net is worse than no safety net,
        # because the next reader believes it.
        matched.turo_listing_id = listing_id
        log.info("learned Turo listing %s for %s", listing_id, matched.nickname)
    return matched


def apply_parsed_trip(
    session: Session, parsed: ParsedTrip, *, now: datetime | None = None
) -> Trip | None:
    """Create or update the trip this email describes.

    Returns None when no vehicle could be matched, because Trip.vehicle_id is
    required — a trip has to belong to a car. Reported rather than dropped
    silently, so an unmatched vehicle name is a thing the operator can fix by
    renaming rather than a mystery.
    """
    now = now or datetime.now(UTC)
    existing = session.scalar(
        select(Trip).where(Trip.turo_trip_id == parsed.reservation_id)
    )

    vehicle = match_vehicle(session, parsed.vehicle_text, listing_id=parsed.turo_listing_id)
    if existing is None and vehicle is None:
        return None

    state = parsed.state
    # The dates outrank the email's label. An "upcoming trip" reminder that
    # arrives after the trip has started should not move it back to upcoming,
    # and a cancellation stays cancelled whatever the dates say.
    if state is not TripState.cancelled:
        if parsed.ends_at <= now:
            state = TripState.completed
        elif parsed.starts_at <= now:
            state = TripState.active

    if existing is None:
        assert vehicle is not None
        trip = Trip(
            vehicle_id=vehicle.id,
            turo_trip_id=parsed.reservation_id,
            guest_name=parsed.guest_name,
            starts_at=parsed.starts_at,
            ends_at=parsed.ends_at,
            state=state,
            source=TripSource.email,
            synced_at=now,
            earnings_cents=parsed.earnings_cents,
        )
        session.add(trip)
        session.flush()
        return trip

    # A cancelled trip stays cancelled: a later message notification about the
    # same reservation still carries the old dates and would otherwise revive it.
    if existing.state is TripState.cancelled and parsed.state is not TripState.cancelled:
        existing.synced_at = now
        session.flush()
        return existing

    existing.starts_at = parsed.starts_at
    existing.ends_at = parsed.ends_at
    existing.state = state
    existing.synced_at = now
    if parsed.guest_name:
        existing.guest_name = parsed.guest_name
    if parsed.earnings_cents is not None:
        existing.earnings_cents = parsed.earnings_cents
    if vehicle is not None:
        existing.vehicle_id = vehicle.id
    session.flush()
    return existing
