"""Rentals this fleet made outside Turo.

Turo's own rentals arrive by email and nothing here has to be typed. A rental
arranged directly leaves no trace at all, which until now meant its tolls could
not be attributed to anybody: the crossing was real, the guest was real, and
the ledger showed money nobody owed.

So this is a small amount of typing to let the rest of the machinery work. A
manual trip is an ordinary ``Trip`` with ``source=manual`` — the same table the
toll matcher, the run sheet and the turnaround logic already read — rather than
a parallel notion of a rental that each of them would have to learn about.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from turonomics_api.db.base import get_session
from turonomics_api.db.models import Toll, Trip, TripSource, TripState, Vehicle
from turonomics_api.ingest.tolls import rematch_unattributed
from turonomics_api.routers.tolls import require_token
from turonomics_api.settings import fleet_timezone

router = APIRouter(prefix="/api/trips", tags=["trips"])

DbSession = Annotated[Session, Depends(get_session)]


class TripIn(BaseModel):
    """A rental to record. Times without a zone are fleet-local."""

    vehicle: str = Field(min_length=1, description="Nickname or plate")
    guest_name: str | None = None
    starts_at: datetime
    ends_at: datetime
    earnings_cents: int | None = Field(default=None, ge=0)

    @field_validator("starts_at", "ends_at")
    @classmethod
    def _localise(cls, value: datetime) -> datetime:
        """A bare "2026-10-04T14:00" is 2pm where the cars are.

        An HTML datetime-local input sends exactly that, with no zone. Reading
        it as UTC is the mistake that put every scraped toll four hours early,
        and it would be the same mistake here — against a window that decides
        who gets billed.
        """
        if value.tzinfo is None:
            return value.replace(tzinfo=fleet_timezone())
        return value


class TripRow(BaseModel):
    id: uuid.UUID
    vehicle_nickname: str | None
    guest_name: str | None
    starts_at: datetime
    ends_at: datetime
    source: str
    earnings_cents: int | None = None
    # How many crossings this rental is currently carrying, so a window typed
    # slightly wrong is visible as a rental that caught nothing.
    toll_count: int = 0


class TripsResponse(BaseModel):
    trips: list[TripRow]


class CreateResponse(BaseModel):
    trip: TripRow
    # Crossings that became attributable the moment this rental existed. The
    # whole point of typing it in, so it is reported rather than left to be
    # noticed.
    tolls_matched: int


def _vehicle_for(session: Session, name: str) -> Vehicle:
    wanted = name.strip()
    found = session.scalar(
        select(Vehicle).where(
            func.lower(Vehicle.nickname) == wanted.lower()
        )
    )
    if found is None:
        found = session.scalar(
            select(Vehicle).where(
                func.replace(func.upper(Vehicle.plate), " ", "")
                == wanted.upper().replace(" ", "")
            )
        )
    if found is None:
        known = [
            v.nickname or v.plate or str(v.id) for v in session.scalars(select(Vehicle))
        ]
        raise HTTPException(
            422, f"no car called {name!r}. Known: {', '.join(sorted(filter(None, known)))}"
        )
    return found


def _row(session: Session, trip: Trip) -> TripRow:
    count = (
        session.scalar(
            select(func.count()).select_from(Toll).where(Toll.trip_id == trip.id)
        )
        or 0
    )
    return TripRow(
        id=trip.id,
        vehicle_nickname=trip.vehicle.nickname if trip.vehicle else None,
        guest_name=trip.guest_name,
        starts_at=trip.starts_at,
        ends_at=trip.ends_at,
        source=str(trip.source),
        earnings_cents=trip.earnings_cents,
        toll_count=int(count),
    )


@router.get("", response_model=TripsResponse)
def list_trips(session: DbSession, manual_only: bool = True) -> TripsResponse:
    """Rentals, newest first. Manual ones by default.

    The Turo ones are not editable here and there are hundreds of them; what
    this list is for is seeing and correcting what was typed in by hand.
    """
    query = select(Trip).order_by(Trip.starts_at.desc()).limit(200)
    if manual_only:
        query = query.where(Trip.source == TripSource.manual)
    return TripsResponse(trips=[_row(session, t) for t in session.scalars(query)])


@router.post("", response_model=CreateResponse)
def create_trip(
    body: TripIn,
    session: DbSession,
    authorization: Annotated[str | None, Header()] = None,
) -> CreateResponse:
    """Record an off-platform rental, then attribute whatever it now covers."""
    require_token(authorization)
    if body.ends_at <= body.starts_at:
        # Caught here as well as by the table's check constraint, because the
        # constraint's error is a stack trace and this one is a sentence.
        raise HTTPException(422, "the rental has to end after it starts")

    vehicle = _vehicle_for(session, body.vehicle)
    now = datetime.now(tz=fleet_timezone())
    trip = Trip(
        vehicle_id=vehicle.id,
        guest_name=(body.guest_name or "").strip() or None,
        starts_at=body.starts_at,
        ends_at=body.ends_at,
        state=(
            TripState.completed
            if body.ends_at < now
            else TripState.active
            if body.starts_at <= now
            else TripState.upcoming
        ),
        source=TripSource.manual,
        earnings_cents=body.earnings_cents,
        synced_at=now,
    )
    session.add(trip)
    session.flush()

    matched = rematch_unattributed(session)
    session.commit()
    return CreateResponse(trip=_row(session, trip), tolls_matched=matched)


@router.delete("/{trip_id}")
def delete_trip(
    trip_id: uuid.UUID,
    session: DbSession,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, int]:
    """Remove a manual rental typed in wrongly.

    Only a manual one. The Turo rentals are a record of what happened, rebuilt
    from mail, and deleting one here would be undone by the next sync anyway.

    Crossings it had claimed go back to being unattributed rather than being
    deleted with it — the toll still happened.
    """
    require_token(authorization)
    trip = session.get(Trip, trip_id)
    if trip is None:
        # Same reasoning as deleting a toll: a retry after a dropped
        # connection is likelier than a delete aimed at nothing.
        return {"deleted": 0, "tolls_released": 0}
    if trip.source is not TripSource.manual:
        raise HTTPException(
            422, "only a manually recorded rental can be deleted here"
        )
    # Counted before the delete, not cleared by hand: toll.trip_id is declared
    # ON DELETE SET NULL, so the crossings are released by the database. A loop
    # doing it here too looked load-bearing and was not — mutating it away
    # changed nothing. What keeps that honest is the test asserting the tolls
    # still exist afterwards, which is what would fail if the constraint ever
    # became CASCADE.
    released = (
        session.scalar(
            select(func.count()).select_from(Toll).where(Toll.trip_id == trip.id)
        )
        or 0
    )
    session.delete(trip)
    session.commit()
    return {"deleted": 1, "tolls_released": int(released)}
