"""Commands the site queues for the browser extension.

The site cannot talk to Turo; only the operator's browser holds that session.
So a button on the site queues a command here, and the extension — asking
every few seconds while Chrome is open — claims it, runs it, and reports.

Two kinds, both things the extension's popup already does:

* ``pull`` — read Turo's trips and invoices.
* ``file`` — file the toll invoice for one rental.

What keeps this from being a way to bill a guest twice:

* A filing is only queued for a rental the ledger would file, by the same rule
  next-draft uses, and checked again by the extension when it runs.
* A second click while one is queued or running returns the same command.
* A filing claimed and never reported is never retried. It may have filed and
  lost its answer; it is marked abandoned and left for a person.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from turonomics_api.db.base import get_session
from turonomics_api.db.models import ExtensionCheckin, ExtensionCommand, Trip
from turonomics_api.routers.invoices import fileable_invoice
from turonomics_api.routers.tolls import require_token, token_configured

log = logging.getLogger("turonomics.routers.commands")

router = APIRouter(prefix="/api/commands", tags=["commands"])

DbSession = Annotated[Session, Depends(get_session)]

KINDS = frozenset({"pull", "file"})
QUEUED, RUNNING, DONE, FAILED, ABANDONED = "queued", "running", "done", "failed", "abandoned"

# A filing takes seconds and a full pull under a minute. Ten minutes without a
# report means the browser closed or the worker died mid-command.
STALE_AFTER = timedelta(minutes=10)

# The extension asks every few seconds, kept awake by an offscreen document,
# with a thirty-second alarm behind it. Past this it is not listening.
LISTENING_WITHIN = timedelta(minutes=2)
CHECKIN_EVERY = timedelta(seconds=20)


class CommandIn(BaseModel):
    kind: str
    trip_id: uuid.UUID | None = None


class CommandOut(BaseModel):
    id: uuid.UUID
    kind: str
    trip_id: uuid.UUID | None
    turo_trip_id: str | None
    guest_name: str | None
    state: str
    requested_at: datetime
    claimed_at: datetime | None
    finished_at: datetime | None
    result: str | None


class CommandsResponse(BaseModel):
    commands: list[CommandOut]
    # Whether an extension has checked in lately, so the site can say before a
    # click whether anything will pick it up.
    extension_seen_at: datetime | None
    extension_version: str | None
    listening: bool
    token_required: bool


class DoneIn(BaseModel):
    ok: bool
    result: str


def _out(command: ExtensionCommand) -> CommandOut:
    trip = command.trip
    return CommandOut(
        id=command.id,
        kind=command.kind,
        trip_id=command.trip_id,
        turo_trip_id=trip.turo_trip_id if trip is not None else None,
        guest_name=trip.guest_name if trip is not None else None,
        state=command.state,
        requested_at=command.requested_at,
        claimed_at=command.claimed_at,
        finished_at=command.finished_at,
        result=command.result,
    )


def _abandon_stale(session: Session, now: datetime) -> None:
    for command in session.scalars(
        select(ExtensionCommand).where(
            ExtensionCommand.state == RUNNING,
            ExtensionCommand.claimed_at < now - STALE_AFTER,
        )
    ):
        command.state = ABANDONED
        command.finished_at = now
        command.result = (
            "the browser never reported back — check Turo before filing this again"
            if command.kind == "file"
            else "the browser never reported back"
        )
        log.warning("command %s (%s) abandoned", command.id, command.kind)


@router.get("", response_model=CommandsResponse)
def list_commands(session: DbSession, limit: int = 10) -> CommandsResponse:
    now = datetime.now(UTC)
    _abandon_stale(session, now)
    session.commit()
    commands = session.scalars(
        select(ExtensionCommand)
        .order_by(ExtensionCommand.requested_at.desc())
        .limit(max(1, min(limit, 50)))
    ).all()
    checkin = session.get(ExtensionCheckin, 1)
    return CommandsResponse(
        commands=[_out(c) for c in commands],
        extension_seen_at=checkin.seen_at if checkin else None,
        extension_version=checkin.version if checkin else None,
        listening=checkin is not None and now - checkin.seen_at <= LISTENING_WITHIN,
        token_required=token_configured(),
    )


@router.post("", response_model=CommandOut)
def queue_command(
    payload: CommandIn,
    session: DbSession,
    authorization: Annotated[str | None, Header()] = None,
) -> CommandOut:
    require_token(authorization)
    if payload.kind not in KINDS:
        raise HTTPException(status_code=422, detail=f"no such command: {payload.kind}")
    if payload.kind == "file":
        if payload.trip_id is None:
            raise HTTPException(status_code=422, detail="which rental?")
        trip = session.get(Trip, payload.trip_id)
        if trip is None or not trip.turo_trip_id:
            raise HTTPException(status_code=404, detail="no such Turo rental")
        _, held = fileable_invoice(session, payload.trip_id)
        if held is not None:
            # Refused here rather than left for the extension to refuse, so the
            # person clicking learns why while they are looking at the row.
            raise HTTPException(status_code=409, detail=held)
    elif payload.trip_id is not None:
        raise HTTPException(status_code=422, detail="a pull is not for one rental")

    # A second click while one is waiting is the same request. For a filing
    # this is the double-click that would otherwise ask a guest twice.
    pending = session.scalar(
        select(ExtensionCommand).where(
            ExtensionCommand.kind == payload.kind,
            ExtensionCommand.trip_id == payload.trip_id
            if payload.trip_id is not None
            else ExtensionCommand.trip_id.is_(None),
            ExtensionCommand.state.in_([QUEUED, RUNNING]),
        )
    )
    if pending is not None:
        return _out(pending)

    command = ExtensionCommand(
        kind=payload.kind,
        trip_id=payload.trip_id,
        state=QUEUED,
        requested_at=datetime.now(UTC),
    )
    session.add(command)
    session.commit()
    log.info("command %s queued: %s %s", command.id, command.kind, payload.trip_id or "")
    return _out(command)


@router.post("/claim", response_model=CommandOut, responses={204: {"description": "nothing"}})
def claim(
    session: DbSession,
    authorization: Annotated[str | None, Header()] = None,
    x_extension_version: Annotated[str | None, Header()] = None,
) -> CommandOut | Response:
    """The oldest queued command, now running — or 204 if there is none.

    Called every few seconds by every browser with the extension, so it is
    also the check-in. Claimed with a row lock that skips locked rows, so two
    browsers never run one command.
    """
    require_token(authorization)
    now = datetime.now(UTC)
    checkin = session.get(ExtensionCheckin, 1)
    if checkin is None:
        session.add(ExtensionCheckin(id=1, seen_at=now, version=x_extension_version))
    elif now - checkin.seen_at >= CHECKIN_EVERY or (
        x_extension_version and x_extension_version != checkin.version
    ):
        # Not on every call: the extension asks every few seconds, and a row
        # rewritten that often says nothing a write every half-minute does not.
        checkin.seen_at = now
        checkin.version = x_extension_version or checkin.version
    _abandon_stale(session, now)

    command = session.scalar(
        select(ExtensionCommand)
        .where(ExtensionCommand.state == QUEUED)
        .order_by(ExtensionCommand.requested_at)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    if command is None:
        session.commit()
        return Response(status_code=204)
    command.state = RUNNING
    command.claimed_at = now
    session.commit()
    log.info("command %s claimed: %s", command.id, command.kind)
    return _out(command)


@router.post("/{command_id}/done", response_model=CommandOut)
def done(
    command_id: uuid.UUID,
    payload: DoneIn,
    session: DbSession,
    authorization: Annotated[str | None, Header()] = None,
) -> CommandOut:
    require_token(authorization)
    command = session.get(ExtensionCommand, command_id)
    if command is None:
        raise HTTPException(status_code=404, detail="no such command")
    if command.state not in (RUNNING, ABANDONED):
        # A report for a command that was never claimed, or reported twice.
        raise HTTPException(status_code=409, detail=f"that command is {command.state}")
    # Accepted even after it was given up on: a late answer is still the truth
    # about what happened, and for a filing it is the part that matters.
    command.state = DONE if payload.ok else FAILED
    command.finished_at = datetime.now(UTC)
    command.result = payload.result[:2000]
    session.commit()
    log.info("command %s %s: %s", command.id, command.state, command.result)
    return _out(command)
