"""Reconciling an EZPass statement against who was driving.

Its own router and its own page, because this is not the run sheet. The fleet
view answers "where are my cars and what has to happen now" and refreshes every
minute; a toll statement is something you sit down with once a month. Putting
the two on one screen would make the urgent thing share space with the
back-office thing, to the benefit of neither.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from turonomics_api.db.base import get_session
from turonomics_api.db.models import Toll
from turonomics_api.ingest.tolls import import_tolls, rematch_unattributed

router = APIRouter(prefix="/api/tolls", tags=["tolls"])

DbSession = Annotated[Session, Depends(get_session)]

# A statement is a few hundred rows. Enough that the page is not paginated,
# capped so a mistaken upload cannot ask the database for everything at once.
DEFAULT_LIMIT = 500


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


class TollsResponse(BaseModel):
    tolls: list[TollRow]
    # Totals in cents, for the same reason they are stored that way: a figure
    # the operator is going to bill somebody should not be the sum of floats.
    total_cents: int
    unrecovered_cents: int
    unattributed_cents: int
    unknown_tags: list[str]


class ImportResponse(BaseModel):
    rows: int
    imported: int
    already_known: int
    matched: int
    unmatched: int
    unknown_tags: list[str]


def _row(toll: Toll) -> TollRow:
    return TollRow(
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
    unattributed = (
        session.scalar(
            select(func.coalesce(func.sum(Toll.amount_cents), 0)).where(Toll.trip_id.is_(None))
        )
        or 0
    )
    unknown = session.scalars(
        select(Toll.transponder_id)
        .where(Toll.vehicle_id.is_(None), Toll.transponder_id.is_not(None))
        .distinct()
    ).all()

    return TollsResponse(
        tolls=[_row(t) for t in rows],
        total_cents=int(total),
        unrecovered_cents=int(unrecovered),
        unattributed_cents=int(unattributed),
        unknown_tags=sorted(t for t in unknown if t),
    )


@router.post("/import", response_model=ImportResponse)
async def import_statement(session: DbSession, statement: UploadFile) -> ImportResponse:
    """Read an EZPass account-activity CSV and attribute every crossing.

    Only the statement. The Turo half of the old two-file upload is redundant
    now that trips are stored with their guests and windows.
    """
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
def rematch(session: DbSession) -> ImportResponse:
    """Re-attribute tolls after binding a transponder or fixing a plate.

    Re-importing the statement will not do it: those rows are already known, so
    the importer skips them and nothing changes. This is the thing to call once
    a tag has a car.
    """
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


@router.post("/{toll_id}/recovered", response_model=TollRow)
def mark_recovered(toll_id: uuid.UUID, session: DbSession, undo: bool = False) -> TollRow:
    """Tick a toll off once it has been billed back, or untick it."""
    toll = session.get(Toll, toll_id)
    if toll is None:
        raise HTTPException(404, "no toll with that id")
    toll.recovered_at = None if undo else datetime.now(UTC)
    session.commit()
    return _row(toll)
