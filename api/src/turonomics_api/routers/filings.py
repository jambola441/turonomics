"""Filing proposals: Claude suggests filing a rental's tolls, a person files.

A filing asks a guest for money on Turo and cannot be taken back, so the MCP
endpoint does not file. It proposes. The proposal shows on the invoices page
beside the rental's File button; pressing File is the approval, refused by
the same ledger rule as any other press, and Dismiss is the refusal.

The states: proposed -> filed (File pressed on that rental) | dismissed. A
rental has at most one open proposal; proposing it again updates the note.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from turonomics_api.db.base import get_session
from turonomics_api.db.models import ExtensionCommand, FilingProposal, Trip
from turonomics_api.routers.tolls import require_token

log = logging.getLogger("turonomics.routers.filings")

router = APIRouter(prefix="/api/filings", tags=["filings"])

DbSession = Annotated[Session, Depends(get_session)]

PROPOSED, FILED, DISMISSED = "proposed", "filed", "dismissed"
STATES = (PROPOSED, FILED, DISMISSED)

MAX_NOTE = 500


class ProposalOut(BaseModel):
    id: uuid.UUID
    trip_id: uuid.UUID
    guest_name: str | None
    vehicle_nickname: str | None
    turo_trip_id: str | None
    amount_cents: int
    note: str | None
    state: str
    created_by: str
    created_at: datetime
    decided_at: datetime | None
    # Once File was pressed: what the extension did with it.
    filing_state: str | None
    filing_result: str | None


class ProposalsResponse(BaseModel):
    proposals: list[ProposalOut]


def proposal_out(proposal: FilingProposal) -> ProposalOut:
    trip = proposal.trip
    command = proposal.command
    return ProposalOut(
        id=proposal.id,
        trip_id=proposal.trip_id,
        guest_name=trip.guest_name if trip is not None else None,
        vehicle_nickname=trip.vehicle.nickname if trip is not None and trip.vehicle else None,
        turo_trip_id=trip.turo_trip_id if trip is not None else None,
        amount_cents=proposal.amount_cents,
        note=proposal.note,
        state=proposal.state,
        created_by=proposal.created_by,
        created_at=proposal.created_at,
        decided_at=proposal.decided_at,
        filing_state=command.state if command is not None else None,
        filing_result=command.result if command is not None else None,
    )


def propose(
    session: Session, trip_id: uuid.UUID, note: str | None, created_by: str
) -> FilingProposal:
    """An open proposal to file this rental. Raises ValueError with the reason if not.

    Refused on the ledger's word before it reaches anyone: a proposal the
    File button would then refuse is only noise on the page.
    """
    from turonomics_api.routers.invoices import fileable_invoice, ledger

    trip = session.get(Trip, trip_id)
    if trip is None:
        raise ValueError("no rental with that id")
    if not trip.turo_trip_id:
        raise ValueError("an off-platform rental has no Turo reservation to file on")
    _, held = fileable_invoice(session, trip_id)
    if held is not None:
        raise ValueError(f"the ledger would not file it: {held}")
    row = next((r for r in ledger(session).rows if r.trip_id == trip_id), None)
    amount = row.unfiled_cents if row is not None else 0
    text = (note or "").strip()[:MAX_NOTE] or None

    now = datetime.now(UTC)
    existing = session.scalar(
        select(FilingProposal).where(
            FilingProposal.trip_id == trip_id, FilingProposal.state == PROPOSED
        )
    )
    if existing is not None:
        existing.note = text or existing.note
        existing.amount_cents = amount
        session.commit()
        return existing
    proposal = FilingProposal(
        trip_id=trip_id, note=text, amount_cents=amount, state=PROPOSED,
        created_by=created_by, created_at=now,
    )
    session.add(proposal)
    session.commit()
    log.info("filing proposal %s on %s by %s", proposal.id, trip.turo_trip_id, created_by)
    return proposal


def filed_by_hand(session: Session, command: ExtensionCommand) -> None:
    """File was pressed on this rental: its open proposal is approved, by that press."""
    if command.trip_id is None:
        return
    now = datetime.now(UTC)
    for proposal in session.scalars(
        select(FilingProposal).where(
            FilingProposal.trip_id == command.trip_id, FilingProposal.state == PROPOSED
        )
    ):
        proposal.state = FILED
        proposal.decided_at = now
        proposal.command_id = command.id


def list_proposals(
    session: Session, trip_id: uuid.UUID | None = None, state: str | None = None
) -> list[FilingProposal]:
    query = select(FilingProposal).order_by(FilingProposal.created_at.desc()).limit(200)
    if trip_id is not None:
        query = query.where(FilingProposal.trip_id == trip_id)
    if state:
        query = query.where(FilingProposal.state == state)
    return list(session.scalars(query))


@router.get("", response_model=ProposalsResponse)
def get_proposals(
    session: DbSession,
    state: str | None = None,
    authorization: Annotated[str | None, Header()] = None,
) -> ProposalsResponse:
    require_token(authorization)
    if state and state not in STATES:
        raise HTTPException(422, f"state is one of {', '.join(STATES)}")
    return ProposalsResponse(proposals=[proposal_out(p) for p in list_proposals(session, None, state)])


@router.post("/{proposal_id}/dismiss", response_model=ProposalOut)
def dismiss(
    proposal_id: uuid.UUID,
    session: DbSession,
    authorization: Annotated[str | None, Header()] = None,
) -> ProposalOut:
    require_token(authorization)
    proposal = session.get(FilingProposal, proposal_id)
    if proposal is None:
        raise HTTPException(404, "no such proposal")
    if proposal.state != PROPOSED:
        raise HTTPException(409, f"that proposal is {proposal.state}")
    proposal.state = DISMISSED
    proposal.decided_at = datetime.now(UTC)
    session.commit()
    return proposal_out(proposal)
