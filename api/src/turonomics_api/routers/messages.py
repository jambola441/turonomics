"""Messages to a rental's guest: drafted, approved by a person, then sent.

A draft is written on the site or by Claude through the MCP endpoint. It is
sent only when the operator presses Send on it on the site, which queues a
``message`` command; the extension posts it from a Turo tab and reports back.
Every message is approved by a person for now: the MCP tools can draft and
read drafts, and none of them approves one.

The states: draft -> approved -> sent | failed, or draft -> discarded. Only a
draft can be edited, sent or discarded. A failed message is not resent; a new
draft is, if a person decides it should be, because one handed to the browser
and never reported may have gone out.
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
from turonomics_api.db.models import ExtensionCheckin, ExtensionCommand, MessageDraft, Trip
from turonomics_api.routers.commands import (
    MESSAGE,
    MESSAGE_MIN_VERSION,
    QUEUED,
    version_at_least,
)
from turonomics_api.routers.tolls import require_token

log = logging.getLogger("turonomics.routers.messages")

router = APIRouter(prefix="/api/messages", tags=["messages"])

DbSession = Annotated[Session, Depends(get_session)]

DRAFT, APPROVED, SENT, FAILED, DISCARDED = "draft", "approved", "sent", "failed", "discarded"
STATES = (DRAFT, APPROVED, SENT, FAILED, DISCARDED)

# Long enough for any message a host sends; short enough that a runaway draft
# is refused rather than posted to a guest.
MAX_LENGTH = 2000


class DraftIn(BaseModel):
    trip_id: uuid.UUID
    body: str


class DraftEdit(BaseModel):
    body: str


class DraftOut(BaseModel):
    id: uuid.UUID
    trip_id: uuid.UUID
    guest_name: str | None
    vehicle_nickname: str | None
    turo_trip_id: str | None
    body: str
    state: str
    created_by: str
    created_at: datetime
    updated_at: datetime
    approved_at: datetime | None
    sent_at: datetime | None
    result: str | None


class DraftsResponse(BaseModel):
    drafts: list[DraftOut]


def draft_out(draft: MessageDraft) -> DraftOut:
    trip = draft.trip
    return DraftOut(
        id=draft.id,
        trip_id=draft.trip_id,
        guest_name=trip.guest_name if trip is not None else None,
        vehicle_nickname=trip.vehicle.nickname if trip is not None and trip.vehicle else None,
        turo_trip_id=trip.turo_trip_id if trip is not None else None,
        body=draft.body,
        state=draft.state,
        created_by=draft.created_by,
        created_at=draft.created_at,
        updated_at=draft.updated_at,
        approved_at=draft.approved_at,
        sent_at=draft.sent_at,
        result=draft.result,
    )


def _clean(body: str) -> str:
    text = body.strip()
    if not text:
        raise ValueError("the message is empty")
    if len(text) > MAX_LENGTH:
        raise ValueError(f"the message is {len(text)} characters; the most is {MAX_LENGTH}")
    return text


def create_draft(session: Session, trip_id: uuid.UUID, body: str, created_by: str) -> MessageDraft:
    """A new draft on a Turo rental. Raises ValueError with the reason if not."""
    trip = session.get(Trip, trip_id)
    if trip is None:
        raise ValueError("no rental with that id")
    if not trip.turo_trip_id:
        raise ValueError("an off-platform rental has no Turo thread to send to")
    now = datetime.now(UTC)
    draft = MessageDraft(
        trip_id=trip.id, body=_clean(body), state=DRAFT, created_by=created_by,
        created_at=now, updated_at=now,
    )
    session.add(draft)
    session.commit()
    log.info("message draft %s on %s by %s", draft.id, trip.turo_trip_id, created_by)
    return draft


def list_drafts(
    session: Session, trip_id: uuid.UUID | None = None, state: str | None = None
) -> list[MessageDraft]:
    query = select(MessageDraft).order_by(MessageDraft.created_at.desc()).limit(200)
    if trip_id is not None:
        query = query.where(MessageDraft.trip_id == trip_id)
    if state:
        query = query.where(MessageDraft.state == state)
    return list(session.scalars(query))


def _draft(session: Session, draft_id: uuid.UUID) -> MessageDraft:
    draft = session.get(MessageDraft, draft_id)
    if draft is None:
        raise HTTPException(404, "no such draft")
    return draft


def _still_a_draft(draft: MessageDraft) -> None:
    if draft.state != DRAFT:
        raise HTTPException(409, f"that message is {draft.state}, not a draft")


@router.get("", response_model=DraftsResponse)
def get_drafts(
    session: DbSession,
    trip_id: uuid.UUID | None = None,
    state: str | None = None,
    authorization: Annotated[str | None, Header()] = None,
) -> DraftsResponse:
    # Behind the token: guests by name, and what is about to be said to them.
    require_token(authorization)
    if state and state not in STATES:
        raise HTTPException(422, f"state is one of {', '.join(STATES)}")
    return DraftsResponse(drafts=[draft_out(d) for d in list_drafts(session, trip_id, state)])


@router.post("", response_model=DraftOut)
def post_draft(
    payload: DraftIn,
    session: DbSession,
    authorization: Annotated[str | None, Header()] = None,
) -> DraftOut:
    require_token(authorization)
    try:
        return draft_out(create_draft(session, payload.trip_id, payload.body, "site"))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


# A POST rather than a PATCH: the API's CORS rule lets the site send GET and
# POST, and an edit is not worth widening it for.
@router.post("/{draft_id}/edit", response_model=DraftOut)
def edit_draft(
    draft_id: uuid.UUID,
    payload: DraftEdit,
    session: DbSession,
    authorization: Annotated[str | None, Header()] = None,
) -> DraftOut:
    require_token(authorization)
    draft = _draft(session, draft_id)
    _still_a_draft(draft)
    try:
        draft.body = _clean(payload.body)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    draft.updated_at = datetime.now(UTC)
    session.commit()
    return draft_out(draft)


@router.post("/{draft_id}/discard", response_model=DraftOut)
def discard_draft(
    draft_id: uuid.UUID,
    session: DbSession,
    authorization: Annotated[str | None, Header()] = None,
) -> DraftOut:
    require_token(authorization)
    draft = _draft(session, draft_id)
    _still_a_draft(draft)
    draft.state = DISCARDED
    draft.updated_at = datetime.now(UTC)
    session.commit()
    return draft_out(draft)


@router.post("/{draft_id}/send", response_model=DraftOut)
def send_draft(
    draft_id: uuid.UUID,
    session: DbSession,
    authorization: Annotated[str | None, Header()] = None,
) -> DraftOut:
    """The approval: a person pressing Send. Queues the message for the extension.

    Refused while the extension that has checked in cannot send, rather than
    queued for a browser that would fail it. A second press on a message
    already approved returns it as it is: the double-click that would send it
    twice.
    """
    require_token(authorization)
    draft = _draft(session, draft_id)
    if draft.state == APPROVED:
        return draft_out(draft)
    _still_a_draft(draft)
    checkin = session.get(ExtensionCheckin, 1)
    if checkin is None or not version_at_least(checkin.version, MESSAGE_MIN_VERSION):
        wanted = ".".join(str(p) for p in MESSAGE_MIN_VERSION)
        raise HTTPException(
            409,
            f"sending needs extension {wanted} or later"
            + (f"; the one checking in is {checkin.version}" if checkin and checkin.version
               else ""),
        )
    now = datetime.now(UTC)
    command = ExtensionCommand(kind=MESSAGE, trip_id=draft.trip_id, state=QUEUED,
                               requested_at=now)
    session.add(command)
    session.flush()
    draft.state = APPROVED
    draft.approved_at = now
    draft.updated_at = now
    draft.command_id = command.id
    session.commit()
    log.info("message draft %s approved; command %s queued", draft.id, command.id)
    return draft_out(draft)
