"""Work out who was driving when each toll was charged.

The matcher this calls has been in the repo since before any of the rest of
it, taking two uploaded CSVs and matching them in memory. That made sense when
there were no stored trips. There are now months of them, with guest names and
exact windows, so the Turo half of that upload is redundant: a statement alone
is enough.

What this adds on top is persistence, and the reason is the question being
asked. "What does this statement say" is answerable from a file. "What have I
not billed back yet" is only answerable if last month's statement is still
around, and that is the question worth money.

A toll that matches nothing is kept, not dropped. It is either a crossing by a
car that is not in this fleet — a personal one on the same account — or a
transponder nobody has bound to a vehicle, and the second is fixable the
moment the operator can see which tag it was.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from turonomics_api.db.models import Toll, Trip, TripState, Vehicle
from turonomics_api.models import EZPassToll
from turonomics_api.parsing.ezpass import parse_ezpass_csv

log = logging.getLogger("turonomics.ingest.tolls")


@dataclass
class ImportResult:
    """What one statement did to the ledger."""

    rows: int = 0
    imported: int = 0
    already_known: int = 0
    matched: int = 0
    unmatched: int = 0
    unknown_tags: set[str] = field(default_factory=set)

    def summary(self) -> str:
        tags = f", {len(self.unknown_tags)} unknown tag(s)" if self.unknown_tags else ""
        return (
            f"{self.rows} row(s), {self.imported} new, {self.already_known} already known, "
            f"{self.matched} matched to a trip, {self.unmatched} unmatched{tags}"
        )


def fingerprint(toll: EZPassToll) -> str:
    """A stable identity for one crossing.

    EZPass's own transaction id when the export carries one. Otherwise a hash
    of the things that make a crossing unique, because importing a statement
    twice must not double what it says is owed — and some exports have no id
    column at all.
    """
    if toll.txn_id:
        return f"txn:{toll.txn_id}"
    raw = "|".join(
        [
            # To the minute, not the second, and that is the whole point.
            #
            # The same crossing reads "05:13:32 PM" in a downloaded statement
            # and "3:19 PM" on the account-activity page the extension scrapes
            # — the website does not render seconds. Hashing the exact time
            # would give one crossing two identities depending on where it came
            # from, so scraping a page and later uploading the official CSV
            # would bill every toll twice. That is the failure this hash exists
            # to prevent, arriving through the front door.
            #
            # The cost is that two charges at one plaza, in one minute, for one
            # amount, against one tag collapse into one. A car cannot cross the
            # same plaza twice in sixty seconds, so such a pair is EZPass
            # billing the same crossing twice rather than two crossings — and
            # if it ever is real, undercounting by one toll beats double-billing
            # a guest for every toll they incurred.
            toll.timestamp.replace(second=0, microsecond=0).isoformat(),
            toll.plaza,
            f"{toll.amount:.2f}",
            toll.transponder_id or "",
            toll.license_plate or "",
        ]
    )
    return "sha:" + hashlib.sha256(raw.encode()).hexdigest()[:40]


def _vehicle_for(session: Session, toll: EZPassToll) -> Vehicle | None:
    """The car this toll was billed against, by plate or by tag."""
    if toll.license_plate:
        found = session.scalar(select(Vehicle).where(Vehicle.plate == toll.license_plate))
        if found is not None:
            return found
    if toll.transponder_id:
        return session.scalar(select(Vehicle).where(Vehicle.ezpass_tag == toll.transponder_id))
    return None


def _trip_at(session: Session, vehicle_id: uuid.UUID, when: datetime) -> Trip | None:
    """The trip that was running at that moment, if one was.

    Narrowest window first. Back-to-back rentals of one car can overlap by a
    few minutes around the handover, and the shorter trip is the more specific
    claim — the same tie-break the original matcher used.
    """
    return session.scalar(
        select(Trip)
        .where(
            Trip.vehicle_id == vehicle_id,
            Trip.state != TripState.cancelled,
            Trip.starts_at <= when,
            Trip.ends_at >= when,
        )
        .order_by((Trip.ends_at - Trip.starts_at).asc())
        .limit(1)
    )


def import_tolls(
    session: Session, content: str | bytes, *, now: datetime | None = None
) -> ImportResult:
    """Read a statement, attribute every crossing, and keep the result."""
    now = now or datetime.now(UTC)
    result = ImportResult()
    tolls = parse_ezpass_csv(content)
    result.rows = len(tolls)

    for toll in tolls:
        key = fingerprint(toll)
        if session.scalar(select(Toll.id).where(Toll.fingerprint == key)) is not None:
            result.already_known += 1
            continue

        vehicle = _vehicle_for(session, toll)
        trip = _trip_at(session, vehicle.id, toll.timestamp) if vehicle else None
        if trip is not None:
            result.matched += 1
        else:
            result.unmatched += 1
            if vehicle is None and toll.transponder_id:
                # Named so it can be bound. A tag the fleet does not recognise
                # is one env var away from matching every toll it ever charges.
                result.unknown_tags.add(toll.transponder_id)

        session.add(
            Toll(
                fingerprint=key,
                occurred_at=toll.timestamp,
                plaza=toll.plaza,
                # Rounded at the boundary, once. Every total after this is
                # integer arithmetic and agrees with itself.
                amount_cents=int(round(toll.amount * 100)),
                transponder_id=toll.transponder_id,
                license_plate=toll.license_plate,
                vehicle_id=vehicle.id if vehicle else None,
                trip_id=trip.id if trip else None,
            )
        )
        result.imported += 1

    session.flush()
    log.info("toll import: %s", result.summary())
    if result.unknown_tags:
        log.info(
            "unrecognised transponder(s): %s — bind with EZPASS_TAGS=<car>=<tag>",
            ", ".join(sorted(result.unknown_tags)),
        )
    return result


def rematch_unattributed(session: Session) -> int:
    """Attach tolls that could not be matched when they were imported.

    Binding a transponder, correcting a plate or back-filling a trip all make
    old tolls matchable, and re-importing the statement will not help because
    the rows are already known. Returns how many found a home.
    """
    fixed = 0
    for toll in session.scalars(select(Toll).where(Toll.trip_id.is_(None))).all():
        vehicle = session.get(Vehicle, toll.vehicle_id) if toll.vehicle_id else None
        if vehicle is None:
            vehicle = _vehicle_for(
                session,
                EZPassToll(
                    timestamp=toll.occurred_at,
                    plaza=toll.plaza,
                    amount=toll.amount_cents / 100,
                    transponder_id=toll.transponder_id,
                    license_plate=toll.license_plate,
                ),
            )
            if vehicle is None:
                continue
            toll.vehicle_id = vehicle.id
        trip = _trip_at(session, vehicle.id, toll.occurred_at)
        if trip is not None:
            toll.trip_id = trip.id
            fixed += 1
    session.flush()
    return fixed
