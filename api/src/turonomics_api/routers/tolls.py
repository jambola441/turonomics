"""Reconciling an EZPass statement against who was driving.

Its own router and its own page, because this is not the run sheet. The fleet
view answers "where are my cars and what has to happen now" and refreshes every
minute; a toll statement is something you sit down with once a month. Putting
the two on one screen would make the urgent thing share space with the
back-office thing, to the benefit of neither.
"""

from __future__ import annotations

import hmac
import logging
import os
import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from turonomics_api.db.base import get_session
from turonomics_api.db.models import Toll, Trip, TripState
from turonomics_api.ingest.tolls import (
    import_tolls,
    nearest_trip,
    rematch_unattributed,
)
from turonomics_api.settings import outside_fleet

log = logging.getLogger("turonomics.routers.tolls")

router = APIRouter(prefix="/api/tolls", tags=["tolls"])

DbSession = Annotated[Session, Depends(get_session)]

# A statement is a few hundred rows. Enough that the page is not paginated,
# capped so a mistaken upload cannot ask the database for everything at once.
DEFAULT_LIMIT = 500


def require_token(authorization: str | None) -> None:
    """Enforce ``TOLLS_TOKEN`` on the writes if it is set; allow all if not.

    Open by default for the same reason ``/api/push`` is: the app has no login,
    and an upload button the operator cannot use without first inventing a
    token is worse than one they have to protect. It is a weaker argument here
    than it is there, though, and worth being plain about — a stranger who
    posts a statement does not read anything, they add invented charges to the
    figure this fleet bills its guests. Set the variable.
    """
    expected = os.environ.get("TOLLS_TOKEN", "").strip()
    if not expected:
        return
    supplied = (authorization or "").removeprefix("Bearer ").strip()
    # Constant time: a token is a secret, and a timing oracle is a slow leak.
    if not supplied or not hmac.compare_digest(supplied, expected):
        raise HTTPException(401, "bad or missing token")


def token_configured() -> bool:
    return bool(os.environ.get("TOLLS_TOKEN", "").strip())


class TollRow(BaseModel):
    id: uuid.UUID
    occurred_at: datetime
    plaza: str
    amount_cents: int
    transponder_id: str | None = None
    license_plate: str | None = None
    vehicle_nickname: str | None = None
    guest_name: str | None = None
    trip_id: uuid.UUID | None = None
    recovered_at: datetime | None = None
    # Whose car it is, when the crossing belongs to one that is not this
    # fleet's. None for everything else.
    outside_label: str | None = None
    # The nearest rental to a crossing no rental contains, as a hint about
    # whose it might have been. Set only for an unattributed crossing on a car
    # this fleet owns; never an attribution.
    near_guest: str | None = None
    near_gap_seconds: int | None = None
    near_relation: str | None = None
    near_trip_id: uuid.UUID | None = None
    # Set when this crossing is billed to a rental it happened *after* — a late
    # return. Derived rather than stored: the crossing's own time against the
    # rental's end says it exactly. Surfaced because it is an inference about
    # whose money this is, and the operator should be able to see and overrule
    # it rather than find a guest billed for a toll on a hunch.
    overrun_seconds: int | None = None


class TollsResponse(BaseModel):
    tolls: list[TollRow]
    # Totals in cents, for the same reason they are stored that way: a figure
    # the operator is going to bill somebody should not be the sum of floats.
    total_cents: int
    unrecovered_cents: int
    unattributed_cents: int
    unknown_tags: list[str]
    # Money on this account that is not this fleet's — a family car, a van that
    # has left. Real money out, but not a guest's to repay and not a gap to be
    # fixed, so it is reported apart from both.
    outside_cents: int = 0
    # Whether the writes need a token. The page asks for one up front rather
    # than discovering it from a failed upload.
    token_required: bool = False


class ImportResponse(BaseModel):
    rows: int
    imported: int
    already_known: int
    matched: int
    unmatched: int
    unknown_tags: list[str]


def _identifier(toll: Toll) -> str:
    """How a statement names this crossing: its tag, or its plate.

    Not upper-cased. The parser already uppercases a plate and a tag is all
    digits, so there is no case left to normalise — a mutation removing an
    ``.upper()`` here passed every test, which is what dead code looks like.
    The variable's own keys are upper-cased where they are read, because those
    are typed by a person.
    """
    return toll.transponder_id or toll.license_plate or ""


def _trips_by_vehicle(session: Session, tolls: list[Toll]) -> dict[uuid.UUID, list[Trip]]:
    """Every rental of every car that has an unattributed crossing, in one query.

    Loaded up front because the alternative is two queries per loose crossing,
    and a statement leaves a hundred of them.
    """
    wanted = {
        t.vehicle_id for t in tolls if t.vehicle_id is not None and t.trip_id is None
    }
    if not wanted:
        return {}
    trips = session.scalars(
        select(Trip).where(
            Trip.vehicle_id.in_(wanted), Trip.state != TripState.cancelled
        )
    ).all()
    out: dict[uuid.UUID, list[Trip]] = {}
    for trip in trips:
        out.setdefault(trip.vehicle_id, []).append(trip)
    return out


def _row(
    toll: Toll,
    outside: dict[str, str] | None = None,
    trips: dict[uuid.UUID, list[Trip]] | None = None,
) -> TollRow:
    outside = outside if outside is not None else outside_fleet()
    near = None
    # Only worth computing for a crossing on one of our cars that nothing has
    # claimed. An attributed one has its answer, and one on a car outside the
    # fleet has no rentals to be near.
    if trips and toll.trip_id is None and toll.vehicle_id is not None:
        near = nearest_trip(trips.get(toll.vehicle_id, ()), toll.occurred_at)
    overrun = None
    if toll.trip is not None and toll.occurred_at > toll.trip.ends_at:
        overrun = int((toll.occurred_at - toll.trip.ends_at).total_seconds())
    return TollRow(
        outside_label=outside.get(_identifier(toll)),
        overrun_seconds=overrun,
        near_guest=near.guest_name if near else None,
        near_gap_seconds=near.gap_seconds if near else None,
        near_relation=near.relation if near else None,
        near_trip_id=near.trip_id if near else None,
        id=toll.id,
        occurred_at=toll.occurred_at,
        plaza=toll.plaza,
        amount_cents=toll.amount_cents,
        transponder_id=toll.transponder_id,
        license_plate=toll.license_plate,
        vehicle_nickname=toll.vehicle.nickname if toll.vehicle else None,
        guest_name=toll.trip.guest_name if toll.trip else None,
        trip_id=toll.trip_id,
        recovered_at=toll.recovered_at,
    )


@router.get("", response_model=TollsResponse)
def list_tolls(
    session: DbSession,
    unrecovered_only: bool = False,
    limit: int = DEFAULT_LIMIT,
) -> TollsResponse:
    """The ledger, newest first.

    Unattributed tolls are included rather than filtered out. One is either a
    crossing by a car outside this fleet or a transponder nobody has bound, and
    hiding it would turn a fixable gap into a silently smaller total.
    """
    query = select(Toll).order_by(Toll.occurred_at.desc()).limit(min(limit, DEFAULT_LIMIT))
    if unrecovered_only:
        query = query.where(Toll.recovered_at.is_(None))
    rows = session.scalars(query).all()
    outside = outside_fleet()
    trips = _trips_by_vehicle(session, list(rows))

    total = session.scalar(select(func.coalesce(func.sum(Toll.amount_cents), 0))) or 0
    unrecovered = (
        session.scalar(
            select(func.coalesce(func.sum(Toll.amount_cents), 0)).where(
                Toll.recovered_at.is_(None)
            )
        )
        or 0
    )
    # Money that cannot be billed to anyone yet, because nothing says whose
    # crossing it was. Reported separately so it is not mistaken for revenue
    # waiting to be collected.
    # Summed in Python rather than SQL because which identifiers are outside
    # the fleet lives in the environment, not the database — so that changing
    # whose car a tag is does not mean re-importing a statement.
    loose = session.scalars(
        select(Toll).where(Toll.trip_id.is_(None))
    ).all()
    unattributed = sum(
        t.amount_cents for t in loose if _identifier(t) not in outside
    )
    outside_total = sum(
        t.amount_cents for t in loose if _identifier(t) in outside
    )

    unknown = session.scalars(
        select(Toll.transponder_id)
        .where(Toll.vehicle_id.is_(None), Toll.transponder_id.is_not(None))
        .distinct()
    ).all()

    return TollsResponse(
        tolls=[_row(t, outside, trips) for t in rows],
        total_cents=int(total),
        unrecovered_cents=int(unrecovered),
        unattributed_cents=int(unattributed),
        # A tag whose owner is known is not an unbound tag. Listing it would
        # ask the operator, every month, to bind a car that does not exist.
        unknown_tags=sorted(t for t in unknown if t and t.upper() not in outside),
        outside_cents=int(outside_total),
        token_required=token_configured(),
    )


@router.post("/import", response_model=ImportResponse)
async def import_statement(
    session: DbSession,
    statement: UploadFile,
    authorization: Annotated[str | None, Header()] = None,
) -> ImportResponse:
    """Read an EZPass account-activity CSV and attribute every crossing.

    Only the statement. The Turo half of the old two-file upload is redundant
    now that trips are stored with their guests and windows.
    """
    require_token(authorization)
    content = await statement.read()
    try:
        result = import_tolls(session, content)
    except ValueError as exc:
        # The parser's message names the missing column and lists what it did
        # find, which is the difference between "fix your file" and "fix what".
        raise HTTPException(422, str(exc)) from exc
    session.commit()
    return ImportResponse(
        rows=result.rows,
        imported=result.imported,
        already_known=result.already_known,
        matched=result.matched,
        unmatched=result.unmatched,
        unknown_tags=sorted(result.unknown_tags),
    )


@router.post("/rematch", response_model=ImportResponse)
def rematch(
    session: DbSession,
    authorization: Annotated[str | None, Header()] = None,
) -> ImportResponse:
    """Re-attribute tolls after binding a transponder or fixing a plate.

    Re-importing the statement will not do it: those rows are already known, so
    the importer skips them and nothing changes. This is the thing to call once
    a tag has a car.
    """
    require_token(authorization)
    fixed = rematch_unattributed(session)
    session.commit()
    still_loose = (
        session.scalar(
            select(func.count()).select_from(Toll).where(Toll.trip_id.is_(None))
        )
        or 0
    )
    return ImportResponse(
        rows=fixed + int(still_loose),
        imported=0,
        already_known=0,
        matched=fixed,
        unmatched=int(still_loose),
        unknown_tags=[],
    )


class DeleteResponse(BaseModel):
    deleted: int


@router.delete("/{toll_id}", response_model=DeleteResponse)
def delete_toll(
    toll_id: uuid.UUID,
    session: DbSession,
    authorization: Annotated[str | None, Header()] = None,
) -> DeleteResponse:
    """Remove one crossing from the ledger.

    Until this existed an import could not be undone. Uploading the wrong file,
    or a page the scraper read badly, left rows that inflate what the operator
    thinks they are owed and nothing short of database access to get rid of
    them — the one direction a money ledger must always have.

    Only the toll row goes. The vehicle and the trip it pointed at are
    untouched, and the crossing can be imported again afterwards because
    deleting the row releases its fingerprint.
    """
    require_token(authorization)
    toll = session.get(Toll, toll_id)
    if toll is None:
        # Deliberately not a 404. A delete is retried after a dropped
        # connection more often than it is sent for a row that never existed,
        # and failing the retry teaches the operator to doubt the first one.
        return DeleteResponse(deleted=0)
    session.delete(toll)
    session.commit()
    log.info("deleted toll %s (%s, %d cents)", toll_id, toll.plaza, toll.amount_cents)
    return DeleteResponse(deleted=1)


@router.post("/{toll_id}/recovered", response_model=TollRow)
def mark_recovered(
    toll_id: uuid.UUID,
    session: DbSession,
    undo: bool = False,
    authorization: Annotated[str | None, Header()] = None,
) -> TollRow:
    """Tick a toll off once it has been billed back, or untick it."""
    require_token(authorization)
    toll = session.get(Toll, toll_id)
    if toll is None:
        raise HTTPException(404, "no toll with that id")
    toll.recovered_at = None if undo else datetime.now(UTC)
    session.commit()
    return _row(toll)
