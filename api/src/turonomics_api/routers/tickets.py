"""NYC parking and camera tickets on the fleet's plates, and whose they are.

Behind the token: a ticket says where a car was and names the rental that had
it. The list carries both what the city says is still owed and what is to be
asked of a guest, which are different numbers: the city's includes penalties
for paying late, the guest's is the fine as written.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from turonomics_api.db.base import get_session
from turonomics_api.db.models import Ticket
from turonomics_api.ingest.tickets import billable_cents, sync_tickets
from turonomics_api.routers.tolls import require_token

router = APIRouter(prefix="/api/tickets", tags=["tickets"])

DbSession = Annotated[Session, Depends(get_session)]


class TicketRow(BaseModel):
    id: uuid.UUID
    summons_number: str
    kind: str
    violation: str
    issued_at: datetime
    time_known: bool
    county: str | None
    vehicle_nickname: str | None
    trip_id: uuid.UUID | None
    guest_name: str | None
    turo_trip_id: str | None
    fine_cents: int
    penalty_cents: int
    paid_cents: int
    # Still owed to the city, penalties and interest included.
    due_cents: int
    # To be asked of the rental's guest: the fine, less any reduction.
    billable_cents: int
    status: str | None
    image_url: str | None
    filed_at: datetime | None
    recovered_at: datetime | None


class TicketsResponse(BaseModel):
    tickets: list[TicketRow]
    # Owed to the city across every ticket.
    due_cents: int
    # On rentals and not yet recovered from the guest.
    to_recover_cents: int
    # No rental claims them: the host's, or a rental not yet recorded.
    unattributed_cents: int
    last_seen_at: datetime | None


def ticket_row(ticket: Ticket) -> TicketRow:
    trip = ticket.trip
    return TicketRow(
        id=ticket.id,
        summons_number=ticket.summons_number,
        kind=ticket.kind,
        violation=ticket.violation,
        issued_at=ticket.issued_at,
        time_known=ticket.time_known,
        county=ticket.county,
        vehicle_nickname=ticket.vehicle.nickname if ticket.vehicle else None,
        trip_id=ticket.trip_id,
        guest_name=trip.guest_name if trip else None,
        turo_trip_id=trip.turo_trip_id if trip else None,
        fine_cents=ticket.fine_cents,
        penalty_cents=ticket.penalty_cents,
        paid_cents=ticket.paid_cents,
        due_cents=ticket.due_cents,
        billable_cents=billable_cents(ticket),
        status=ticket.status,
        image_url=ticket.image_url,
        filed_at=ticket.filed_at,
        recovered_at=ticket.recovered_at,
    )


def list_tickets(session: Session) -> TicketsResponse:
    tickets = list(session.scalars(
        select(Ticket)
        .options(selectinload(Ticket.trip), selectinload(Ticket.vehicle))
        .order_by(Ticket.issued_at.desc())
    ))
    return TicketsResponse(
        tickets=[ticket_row(t) for t in tickets],
        due_cents=sum(t.due_cents for t in tickets),
        to_recover_cents=sum(
            billable_cents(t) for t in tickets if t.trip_id and t.recovered_at is None
        ),
        unattributed_cents=sum(billable_cents(t) for t in tickets if t.trip_id is None),
        last_seen_at=max((t.last_seen_at for t in tickets), default=None),
    )


@router.get("", response_model=TicketsResponse)
def get_tickets(
    session: DbSession, authorization: Annotated[str | None, Header()] = None
) -> TicketsResponse:
    require_token(authorization)
    return list_tickets(session)


@router.post("/sync", response_model=TicketsResponse)
def sync_now(
    session: DbSession, authorization: Annotated[str | None, Header()] = None
) -> TicketsResponse:
    """Read the city's data now rather than at the next scheduled read."""
    require_token(authorization)
    sync_tickets(session, force=True)
    return list_tickets(session)


@router.post("/{ticket_id}/recovered", response_model=TicketRow)
def mark_recovered(
    ticket_id: uuid.UUID,
    session: DbSession,
    undo: bool = False,
    authorization: Annotated[str | None, Header()] = None,
) -> TicketRow:
    """Tick a ticket off once the guest has paid it back, or untick it."""
    require_token(authorization)
    ticket = session.get(Ticket, ticket_id)
    if ticket is None:
        raise HTTPException(404, "no ticket with that id")
    ticket.recovered_at = None if undo else datetime.now(UTC)
    session.commit()
    return ticket_row(ticket)
