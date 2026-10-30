"""The API as an MCP server, read-only, for asking Claude about the books.

Most questions about this fleet are questions about its own data — what is
left to file, why a rental's miles disagree, what Turo has charged — and were
being answered by reading code, the site and a browser. This answers them
from the API itself, with the same functions the pages use, so the answers
cannot drift from what the site shows.

The protocol is MCP's streamable HTTP transport in its plainest form: one
endpoint, a JSON-RPC request in each POST, a JSON response back, no session
and no server-sent events. That subset is all a tool server needs, and it is
small enough to implement here, under test, rather than take on an SDK. A GET
is refused with 405, which the transport defines as "this server offers no
event stream".

It reads, and it writes what can be undone or has no effect outside the
books. The operator's rule: a write that can be reversed is fine, and one that
cannot waits for a person.

* Done directly: confirming which side of the street a car is on, ticking a
  task done or back, marking Turo's charge on a rental checked, asking the
  extension to pull from Turo or read E-ZPass (both only read), and changing
  Turo's own listing and account settings that no guest books against, each
  change recorded with what it replaced so it can be put back.
* Filed directly, by the operator's decision: a rental's toll reimbursement,
  through the same ledger check as the site's File button, and only once the
  E-ZPass data reaches past the trip (see ``tool_file_toll_invoice``).
* Proposed, never done: a message to a guest is a draft until the operator
  presses Send on the site. There is no tool that sends a message, approves
  anything, or marks money recovered.

Behind ``TOLLS_TOKEN`` like the pages that name guests, and stricter than
they are: those let every request through when the token is unset, and this
refuses to answer at all, because what it hands over is every guest's name
and what each owes, to a client that reads it without a person looking.
"""

from __future__ import annotations

import hmac
import json
import os
import uuid
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Any
from urllib.parse import urljoin

from fastapi import APIRouter, Body, Depends, Header
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from turonomics_api.db.base import get_session

router = APIRouter(tags=["mcp"])

DbSession = Annotated[Session, Depends(get_session)]

# The protocol revisions this server speaks. The client's is echoed back when
# it is one of these; otherwise the newest is offered and the client decides.
PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")

INSTRUCTIONS = (
    "Turonomics keeps the books for a small Turo fleet in New York: every rental, "
    "the E-ZPass crossings attributed to each, what Turo has been asked for and has "
    "charged, and additional distance over each rental's limit. Amounts are integer "
    "cents. Turo takes a toll or distance filing for 90 days after a trip ends; "
    "days_left is what remains of that window (negative once it has closed). "
    "Start with next_to_file for what to do, find_trips to locate a rental by guest, "
    "car, reservation number or date, and trip for everything about one rental. "
    "draft_message writes a message to a rental's guest as a draft; it is not sent "
    "until the operator reads it and presses Send on the site, so say that it is a "
    "draft and is waiting for them. file_toll_invoice files a rental's tolls on Turo; "
    "start from next_to_file, and refresh E-ZPass first with read_ezpass if the newest "
    "crossings are old. propose_filing instead puts a rental in front of the operator "
    "on the invoices page for them to file. fleet shows each car, where it is parked and its "
    "tasks; confirm_parking, complete_task, reopen_task and mark_charge_checked change "
    "the books directly and can be undone. pull_from_turo and read_ezpass ask the "
    "operator's browser extension to refresh the data; they only read. "
    "read_turo_setting and change_turo_setting read and change Turo's own listing and "
    "account settings (guest instructions, description, extras, trip rules, business "
    "hours and the like) through the extension; every change reports before and after "
    "and undo_turo_change puts it back. Read a setting before changing it."
)


class _ToolError(Exception):
    """A tool's own refusal, said to the model as a result rather than a fault."""


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------
# Each takes the session and its arguments and returns plain JSON-able data.
# The route functions are called with the token the request carried, so the
# token check inside each is the one the site's requests go through.

_FILEABLE = ("to bill", "partly billed")


def _dump(model: BaseModel, *, exclude: set[str] | None = None) -> dict[str, Any]:
    data: dict[str, Any] = model.model_dump(mode="json", exclude=exclude)
    return data


def _uuid(value: Any, name: str = "trip_id") -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError) as exc:
        raise _ToolError(f"{name} must be a trip id (a UUID); find_trips gives them") from exc


def _date(value: Any, name: str) -> date | None:
    if value in (None, ""):
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError as exc:
        raise _ToolError(f"{name} must be a date, YYYY-MM-DD") from exc


def _limit(value: Any, default: int, most: int) -> int:
    if value is None:
        return default
    try:
        return max(1, min(int(value), most))
    except (TypeError, ValueError) as exc:
        raise _ToolError("limit must be a number") from exc


def tool_next_to_file(session: Session, auth: str, args: dict[str, Any]) -> dict[str, Any]:
    from turonomics_api.routers.invoices import ledger

    data = ledger(session)
    rows: list[dict[str, Any]] = []
    for row in data.rows:
        if row.days_left is None or row.days_left < 0:
            continue
        tolls = row.unfiled_cents if row.state in _FILEABLE else 0
        distance = row.distance_unbilled_cents or 0
        if not tolls and not distance:
            continue
        rows.append(
            {
                "trip_id": str(row.trip_id),
                "guest_name": row.guest_name,
                "vehicle": row.vehicle_nickname,
                "reservation": row.turo_trip_id,
                "ended": row.ends_at.date().isoformat(),
                "days_left": row.days_left,
                "state": row.state,
                "tolls_to_file_cents": tolls,
                "distance_to_file_cents": distance,
                "note": row.note,
            }
        )
    rows.sort(key=lambda r: int(r["days_left"]))
    limit = _limit(args.get("limit"), 25, 200)
    return {
        "rentals": rows[:limit],
        "more": max(len(rows) - limit, 0),
        "collectable_cents": data.collectable_cents,
        "awaiting_payment_cents": data.awaiting_cents,
        "distance_collectable_cents": data.distance_collectable_cents,
    }


def tool_ledger(session: Session, auth: str, args: dict[str, Any]) -> dict[str, Any]:
    from turonomics_api.routers.invoices import ledger

    data = ledger(session)
    state = (args.get("state") or "").strip().lower()
    guest = (args.get("guest") or "").strip().lower()
    quiet = bool(args.get("include_quiet"))
    rows = []
    for row in data.rows:
        if state and row.state.lower() != state:
            continue
        if guest and guest not in (row.guest_name or "").lower():
            continue
        # A rental with nothing on it from either side, unless asked for.
        nothing = (
            row.state == "no crossings"
            and not row.charged_cents
            and not row.asked_cents
            and not row.turo_unknown_cents
            and not row.distance_unbilled_cents
        )
        if nothing and not quiet:
            continue
        rows.append(_dump(row))
    limit = _limit(args.get("limit"), 50, 300)
    return {
        "rows": rows[:limit],
        "more": max(len(rows) - limit, 0),
        "totals": _dump(data, exclude={"rows"}),
    }


def tool_find_trips(session: Session, auth: str, args: dict[str, Any]) -> dict[str, Any]:
    from turonomics_api.routers.trips import list_trips

    query = (args.get("query") or "").strip().lower()
    start = _date(args.get("from"), "from")
    end = _date(args.get("to"), "to")
    found = []
    for trip in list_trips(session, manual_only=False, authorization=auth).trips:
        hay = " ".join(
            filter(None, [trip.guest_name, trip.vehicle_nickname, trip.turo_trip_id])
        ).lower()
        if query and query not in hay:
            continue
        # Overlapping the range, not inside it: "trips in August" includes
        # one that started on July 30.
        if start and trip.ends_at.date() < start:
            continue
        if end and trip.starts_at.date() > end:
            continue
        found.append(_dump(trip))
    limit = _limit(args.get("limit"), 25, 200)
    return {"trips": found[:limit], "more": max(len(found) - limit, 0)}


def tool_trip(session: Session, auth: str, args: dict[str, Any]) -> dict[str, Any]:
    from fastapi import HTTPException

    from turonomics_api.routers.trips import view_trip

    trip_id = _uuid(args.get("trip_id"))
    try:
        view = view_trip(trip_id, session, authorization=auth)
    except HTTPException as exc:
        if exc.status_code == 404:
            raise _ToolError("no rental with that id") from exc
        raise
    # Turo's raw reservation detail is most of the view by size and adds
    # nothing turo_facts does not already say; the message thread is the
    # guest's own words, sent only when asked for.
    data = _dump(view, exclude={"turo_detail", "messages"})
    messages = view.messages or []
    data["message_count"] = len(messages)
    if args.get("include_messages"):
        data["messages"] = [m.model_dump(mode="json") for m in messages]
    return data


def tool_tolls(session: Session, auth: str, args: dict[str, Any]) -> dict[str, Any]:
    from turonomics_api.routers.tolls import list_tolls

    which = (args.get("filter") or "open").strip().lower()
    if which not in ("open", "no_rental", "all"):
        raise _ToolError("filter is one of open, no_rental, all")
    trip = _uuid(args["trip_id"]) if args.get("trip_id") else None
    data = list_tolls(session, authorization=auth)
    rows = []
    for toll in data.tolls:
        if trip and toll.trip_id != trip:
            continue
        if which == "open" and toll.recovered_at is not None:
            continue
        if which == "no_rental" and (toll.trip_id is not None or toll.outside_label):
            continue
        rows.append(_dump(toll))
    limit = _limit(args.get("limit"), 50, 500)
    return {
        "tolls": rows[:limit],
        "more": max(len(rows) - limit, 0),
        "totals": _dump(data, exclude={"tolls", "token_required"}),
    }


def tool_extension_status(session: Session, auth: str, args: dict[str, Any]) -> dict[str, Any]:
    from turonomics_api.routers.commands import list_commands

    return _dump(list_commands(session, limit=_limit(args.get("limit"), 10, 50)),
                 exclude={"token_required"})


def _draft_view(draft: Any) -> dict[str, Any]:
    from turonomics_api.routers.messages import draft_out
    from turonomics_api.settings import site_url

    data = _dump(draft_out(draft))
    # SITE_URL names the fleet page; the trips page sits beside it.
    data["review_at"] = urljoin(site_url(), "../trips/") + f"#{draft.trip_id}"
    return data


def tool_draft_message(session: Session, auth: str, args: dict[str, Any]) -> dict[str, Any]:
    from turonomics_api.routers.messages import create_draft

    trip_id = _uuid(args.get("trip_id"))
    text = args.get("text")
    if not isinstance(text, str):
        raise _ToolError("text is the message, as a string")
    try:
        draft = create_draft(session, trip_id, text, "mcp")
    except ValueError as exc:
        raise _ToolError(str(exc)) from exc
    data = _draft_view(draft)
    data["sent"] = False
    data["next"] = "Waiting for the operator to read it and press Send on the trip's page."
    return data


def tool_message_drafts(session: Session, auth: str, args: dict[str, Any]) -> dict[str, Any]:
    from turonomics_api.routers.messages import STATES, list_drafts

    state = args.get("state") or None
    if state is not None and state not in STATES:
        raise _ToolError(f"state is one of {', '.join(STATES)}")
    trip = _uuid(args["trip_id"]) if args.get("trip_id") else None
    drafts = list_drafts(session, trip, state)
    limit = _limit(args.get("limit"), 25, 200)
    return {"drafts": [_draft_view(d) for d in drafts[:limit]],
            "more": max(len(drafts) - limit, 0)}


def tool_fleet(session: Session, auth: str, args: dict[str, Any]) -> dict[str, Any]:
    from turonomics_api.routers.fleet import get_fleet

    data = _dump(get_fleet(session, authorization=auth), exclude={"map_tiles"})
    for vehicle in data["vehicles"]:
        # A plate is not needed to talk about a car here, and is the one thing
        # on this page that identifies it to a stranger.
        vehicle.pop("plate", None)
    return data


def _vehicle(state: BaseModel) -> dict[str, Any]:
    data = _dump(state)
    data.pop("plate", None)
    return data


def _not_found(call: Callable[[], BaseModel]) -> dict[str, Any]:
    from fastapi import HTTPException

    try:
        return _vehicle(call())
    except HTTPException as exc:
        if exc.status_code in (404, 409):
            raise _ToolError(str(exc.detail)) from exc
        raise


def tool_confirm_parking(session: Session, auth: str, args: dict[str, Any]) -> dict[str, Any]:
    from turonomics_api.routers.fleet import ConfirmRequest, confirm_parking_side

    parking = _uuid(args.get("session_id"), "session_id")
    side = _uuid(args.get("segment_side_id"), "segment_side_id")
    return _not_found(lambda: confirm_parking_side(
        parking, ConfirmRequest(segment_side_id=side), session, authorization=auth))


def tool_complete_task(session: Session, auth: str, args: dict[str, Any]) -> dict[str, Any]:
    from turonomics_api.routers.fleet import complete_task

    task = _uuid(args.get("task_id"), "task_id")
    return _not_found(lambda: complete_task(task, session, authorization=auth))


def tool_reopen_task(session: Session, auth: str, args: dict[str, Any]) -> dict[str, Any]:
    from turonomics_api.routers.fleet import reopen_task

    task = _uuid(args.get("task_id"), "task_id")
    return _not_found(lambda: reopen_task(task, session, authorization=auth))


def tool_mark_charge_checked(
    session: Session, auth: str, args: dict[str, Any]
) -> dict[str, Any]:
    from fastapi import HTTPException

    from turonomics_api.routers.invoices import mark_charge_checked

    trip_id = _uuid(args.get("trip_id"))
    try:
        out = mark_charge_checked(trip_id, session, undo=bool(args.get("undo")),
                                  authorization=auth)
    except HTTPException as exc:
        if exc.status_code == 404:
            raise _ToolError("no rental with that id") from exc
        raise
    return _dump(out)


def _queue(session: Session, auth: str, kind: str) -> dict[str, Any]:
    from turonomics_api.routers.commands import CommandIn, list_commands, queue_command

    # Only ever "pull" or "tolls": a filing is queued by a person's File press.
    assert kind in ("pull", "tolls")
    command = queue_command(CommandIn(kind=kind), session, authorization=auth)
    status = list_commands(session, limit=1)
    return {
        "command": _dump(command),
        "extension_listening": status.listening,
        "next": (
            "The extension picks it up within seconds; extension_status says when it is done."
            if status.listening else
            "Queued, but no extension is listening: it runs when Chrome is next open "
            "with the extension."
        ),
    }


def tool_pull_from_turo(session: Session, auth: str, args: dict[str, Any]) -> dict[str, Any]:
    return _queue(session, auth, "pull")


def tool_read_ezpass(session: Session, auth: str, args: dict[str, Any]) -> dict[str, Any]:
    return _queue(session, auth, "tolls")


def _proposal_view(proposal: Any) -> dict[str, Any]:
    from turonomics_api.routers.filings import proposal_out
    from turonomics_api.settings import site_url

    data = _dump(proposal_out(proposal))
    data["review_at"] = urljoin(site_url(), "../invoices/") + f"#{proposal.trip_id}"
    return data


# E-ZPass posts a crossing days after it happens. A rental is filed directly
# only once the newest crossing on record is this far past its end, so what
# is filed is all there is; inside the window's last days it is filed with
# what there is rather than lost.
SETTLED_AFTER = timedelta(days=3)
FILE_ANYWAY_DAYS = 7


def tool_file_toll_invoice(session: Session, auth: str, args: dict[str, Any]) -> dict[str, Any]:
    from sqlalchemy import func, select

    from turonomics_api.db.models import ExtensionCommand, Toll, Trip
    from turonomics_api.routers import turo_changes
    from turonomics_api.routers.commands import CommandIn, command_out, queue_command
    from turonomics_api.routers.invoices import fileable_invoice, ledger

    trip_id = _uuid(args.get("trip_id"))
    trip = session.get(Trip, trip_id)
    if trip is None or not trip.turo_trip_id:
        raise _ToolError("no Turo rental with that id")
    _, held = fileable_invoice(session, trip_id)
    if held is not None:
        raise _ToolError(f"the ledger would not file it: {held}")
    row = next((r for r in ledger(session).rows if r.trip_id == trip_id), None)
    days_left = row.days_left if row is not None else None
    newest = session.scalar(select(func.max(Toll.occurred_at)))
    settled = newest is not None and newest >= trip.ends_at + SETTLED_AFTER
    if not settled and (days_left is None or days_left > FILE_ANYWAY_DAYS):
        seen = newest.date().isoformat() if newest else "nothing yet"
        raise _ToolError(
            f"E-ZPass data reaches {seen}; crossings post days late, so this rental is filed "
            f"once it reaches {(trip.ends_at + SETTLED_AFTER).date().isoformat()} "
            "(read_ezpass refreshes it), or in the last "
            f"{FILE_ANYWAY_DAYS} days of the window whatever it reaches"
        )
    command = queue_command(CommandIn(kind="file", trip_id=trip_id), session,
                            authorization=auth)
    row_cmd = session.get(ExtensionCommand, command.id)
    if row_cmd is not None:
        turo_changes.wait(session, row_cmd)
        command = command_out(row_cmd)
    data = _dump(command)
    data["amount_cents"] = row.unfiled_cents if row is not None else None
    data["filed"] = data["state"] == "done"
    if data["state"] in ("queued", "running"):
        data["next"] = "The extension is filing it; extension_status shows the result."
    return data


def tool_propose_filing(session: Session, auth: str, args: dict[str, Any]) -> dict[str, Any]:
    from turonomics_api.routers.filings import propose

    trip_id = _uuid(args.get("trip_id"))
    note = args.get("note")
    if note is not None and not isinstance(note, str):
        raise _ToolError("note is a short reason, as a string")
    try:
        proposal = propose(session, trip_id, note, "mcp")
    except ValueError as exc:
        raise _ToolError(str(exc)) from exc
    data = _proposal_view(proposal)
    data["filed"] = False
    data["next"] = "Waiting for the operator to press File on the invoices page."
    return data


def tool_filing_proposals(session: Session, auth: str, args: dict[str, Any]) -> dict[str, Any]:
    from turonomics_api.routers.filings import STATES, list_proposals

    state = args.get("state") or None
    if state is not None and state not in STATES:
        raise _ToolError(f"state is one of {', '.join(STATES)}")
    trip = _uuid(args["trip_id"]) if args.get("trip_id") else None
    proposals = list_proposals(session, trip, state)
    limit = _limit(args.get("limit"), 25, 200)
    return {"proposals": [_proposal_view(p) for p in proposals[:limit]],
            "more": max(len(proposals) - limit, 0)}


def _turo(session: Session, setting: Any, mode: str, car: Any,
          values: Any) -> dict[str, Any]:
    from turonomics_api.routers import turo_changes

    if not isinstance(setting, str):
        raise _ToolError("setting names one; turo_changes' description lists them")
    if car is not None and not isinstance(car, str):
        raise _ToolError("car is a nickname")
    try:
        command = turo_changes.queue(session, setting, mode, car, values or {})
    except turo_changes.ChangeError as exc:
        raise _ToolError(str(exc)) from exc
    turo_changes.wait(session, command)
    return turo_changes.view(command)


def tool_read_turo_setting(session: Session, auth: str, args: dict[str, Any]) -> dict[str, Any]:
    return _turo(session, args.get("setting"), "read", args.get("car"), {})


def tool_change_turo_setting(
    session: Session, auth: str, args: dict[str, Any]
) -> dict[str, Any]:
    values = args.get("values")
    if not isinstance(values, dict):
        raise _ToolError("values is an object of what to change")
    return _turo(session, args.get("setting"), "write", args.get("car"), values)


def tool_undo_turo_change(session: Session, auth: str, args: dict[str, Any]) -> dict[str, Any]:
    from turonomics_api.routers import turo_changes

    change = _uuid(args.get("change_id"), "change_id")
    try:
        command = turo_changes.undo(session, change)
    except turo_changes.ChangeError as exc:
        raise _ToolError(str(exc)) from exc
    turo_changes.wait(session, command)
    return turo_changes.view(command)


def tool_turo_changes(session: Session, auth: str, args: dict[str, Any]) -> dict[str, Any]:
    from turonomics_api.routers import turo_changes

    limit = _limit(args.get("limit"), 20, 100)
    return {"changes": [turo_changes.view(c) for c in turo_changes.recent(session, limit)]}


def _setting_list() -> str:
    from turonomics_api.routers.turo_changes import SETTINGS

    return " ".join(f"{name} ({spec.scope}): {spec.about}" for name, spec in SETTINGS.items())


def _schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "object", "properties": properties,
                              "additionalProperties": False}
    if required:
        schema["required"] = required
    return schema


_LIMIT = {"type": "integer", "minimum": 1, "description": "At most this many rows."}

Tool = Callable[[Session, str, dict[str, Any]], dict[str, Any]]

# Every writing tool is marked so, and a client that asks before a write asks
# before these. None is destructive: each is undone by another call, or only
# puts something in front of the operator.
WRITES = {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True,
          "openWorldHint": False}

TOOLS: dict[str, tuple[Tool, dict[str, Any]]] = {
    "next_to_file": (tool_next_to_file, {
        "title": "What to file next",
        "description": (
            "Rentals Turo will still take a filing for, soonest deadline first: tolls the "
            "ledger says can be filed and additional distance never invoiced. The "
            "answer to 'what should I do this week'."
        ),
        "inputSchema": _schema({"limit": _LIMIT}),
    }),
    "ledger": (tool_ledger, {
        "title": "The ledger",
        "description": (
            "Every rental with crossings or anything from Turo, and what became of it: "
            "tolls, to bill, asked, settled, what Turo invoiced and charged by kind, "
            "the state (to bill, partly billed, short, expired, settled, awaiting "
            "payment, no crossings, no statement yet) and a note. With totals."
        ),
        "inputSchema": _schema({
            "state": {"type": "string", "description": "Only rows in this state."},
            "guest": {"type": "string", "description": "Only guests whose name contains this."},
            "include_quiet": {"type": "boolean",
                              "description": "Include rentals with nothing on them at all."},
            "limit": _LIMIT,
        }),
    }),
    "find_trips": (tool_find_trips, {
        "title": "Find rentals",
        "description": (
            "Rentals, newest first, Turo's and typed-in: by guest name, car or "
            "reservation number, and by dates the rental overlaps. Gives the trip_id "
            "the other tools take."
        ),
        "inputSchema": _schema({
            "query": {"type": "string",
                      "description": "Part of a guest name, car nickname or reservation."},
            "from": {"type": "string", "description": "YYYY-MM-DD."},
            "to": {"type": "string", "description": "YYYY-MM-DD."},
            "limit": _LIMIT,
        }),
    }),
    "trip": (tool_trip, {
        "title": "One rental",
        "description": (
            "Everything about one rental: its ledger row, crossings, Turo's invoices "
            "and receipt, Turo's facts (odometer, status, price), distance by Turo and "
            "by the tracker, photo counts, and what the site asked the extension to do. "
            "The guest's message thread only with include_messages."
        ),
        "inputSchema": _schema({
            "trip_id": {"type": "string", "description": "From find_trips or the ledger."},
            "include_messages": {"type": "boolean"},
        }, ["trip_id"]),
    }),
    "tolls": (tool_tolls, {
        "title": "Crossings",
        "description": (
            "E-ZPass crossings, newest first, with whose each was: open (not billed "
            "back), no_rental (nobody to bill), or all; optionally one rental's."
        ),
        "inputSchema": _schema({
            "filter": {"type": "string", "enum": ["open", "no_rental", "all"]},
            "trip_id": {"type": "string"},
            "limit": _LIMIT,
        }),
    }),
    "draft_message": (tool_draft_message, {
        "title": "Draft a message to a guest",
        "description": (
            "Write a message to a Turo rental's guest as a draft. It is NOT sent: the "
            "operator reads it on the site and presses Send, and only then does the "
            "browser extension post it to the reservation's thread on Turo. Write it "
            "as the host, in plain text."
        ),
        "inputSchema": _schema({
            "trip_id": {"type": "string", "description": "From find_trips or the ledger."},
            "text": {"type": "string", "description": "The message, at most 2000 characters."},
        }, ["trip_id", "text"]),
        "annotations": {**WRITES, "idempotentHint": False},
    }),
    "message_drafts": (tool_message_drafts, {
        "title": "Message drafts",
        "description": (
            "Messages drafted to guests, newest first, and what became of each: draft "
            "(waiting for the operator), approved (sending), sent, failed, discarded."
        ),
        "inputSchema": _schema({
            "state": {"type": "string",
                      "enum": ["draft", "approved", "sent", "failed", "discarded"]},
            "trip_id": {"type": "string"},
            "limit": _LIMIT,
        }),
    }),
    "extension_status": (tool_extension_status, {
        "title": "The browser extension",
        "description": (
            "Whether the extension in the operator's Chrome is listening, and what it "
            "was last asked to do (pull from Turo, read E-ZPass, file) with the result."
        ),
        "inputSchema": _schema({"limit": _LIMIT}),
    }),
    "fleet": (tool_fleet, {
        "title": "The fleet",
        "description": (
            "Each car now: where it is, fuel, who has it, where it is parked with the "
            "street-cleaning options (each a segment_side_id) and the deadline once a side "
            "is confirmed, and its open tasks (each a task_id)."
        ),
        "inputSchema": _schema({}),
    }),
    "confirm_parking": (tool_confirm_parking, {
        "title": "Confirm which side a car is parked on",
        "description": (
            "Say which street side a parked car is on, from the options fleet lists for "
            "its parking session. This sets the move-by deadline and the move task. "
            "Confirming another side later replaces it."
        ),
        "inputSchema": _schema({
            "session_id": {"type": "string", "description": "The car's parking.session_id."},
            "segment_side_id": {"type": "string",
                                "description": "One of parking.options[].id."},
        }, ["session_id", "segment_side_id"]),
        "annotations": WRITES,
    }),
    "complete_task": (tool_complete_task, {
        "title": "Tick a task done",
        "description": "Mark a car's task done. reopen_task undoes it.",
        "inputSchema": _schema({"task_id": {"type": "string"}}, ["task_id"]),
        "annotations": WRITES,
    }),
    "reopen_task": (tool_reopen_task, {
        "title": "Put a task back",
        "description": "Reopen a task that was ticked done by mistake.",
        "inputSchema": _schema({"task_id": {"type": "string"}}, ["task_id"]),
        "annotations": WRITES,
    }),
    "mark_charge_checked": (tool_mark_charge_checked, {
        "title": "Mark Turo's charge checked",
        "description": (
            "Record that what Turo already charged on a rental has been looked at, which "
            "clears the 'check before asking again' warning until Turo's charge changes. "
            "Changes nothing about what can be filed. undo: true takes it back."
        ),
        "inputSchema": _schema({
            "trip_id": {"type": "string"},
            "undo": {"type": "boolean"},
        }, ["trip_id"]),
        "annotations": WRITES,
    }),
    "pull_from_turo": (tool_pull_from_turo, {
        "title": "Refresh from Turo",
        "description": (
            "Ask the browser extension to read Turo's trips, invoices and receipts. Only "
            "reads. Takes under a minute when the extension is listening."
        ),
        "inputSchema": _schema({}),
        "annotations": WRITES,
    }),
    "read_ezpass": (tool_read_ezpass, {
        "title": "Refresh from E-ZPass",
        "description": "Ask the browser extension to read E-ZPass crossings. Only reads.",
        "inputSchema": _schema({}),
        "annotations": WRITES,
    }),
    "read_turo_setting": (tool_read_turo_setting, {
        "title": "Read a Turo setting",
        "description": (
            "Read one of Turo's listing or account settings through the operator's browser "
            "extension, as Turo has it now, with its options where Turo gives them. Takes "
            "a few seconds. Settings: " + _setting_list()
        ),
        "inputSchema": _schema({
            "setting": {"type": "string"},
            "car": {"type": "string", "description": "The car's nickname, for a car's setting."},
        }, ["setting"]),
    }),
    "change_turo_setting": (tool_change_turo_setting, {
        "title": "Change a Turo setting",
        "description": (
            "Change one of Turo's listing or account settings through the operator's browser "
            "extension. It reads the setting, writes the values given, reads it back and "
            "returns before and after; undo_turo_change puts it back. Read it first. "
            "Settings and their values: " + _setting_list() + " Prices, blocked days, "
            "reservations, money, reviews and messages are not here."
        ),
        "inputSchema": _schema({
            "setting": {"type": "string"},
            "car": {"type": "string", "description": "The car's nickname, for a car's setting."},
            "values": {"type": "object", "description": "What to change, as the setting says."},
        }, ["setting", "values"]),
        "annotations": {**WRITES, "idempotentHint": False},
    }),
    "undo_turo_change": (tool_undo_turo_change, {
        "title": "Undo a Turo change",
        "description": (
            "Put a setting back as it was before a change, from the values the extension "
            "recorded when it made it. The undo is itself a change, and can be undone."
        ),
        "inputSchema": _schema({"change_id": {"type": "string"}}, ["change_id"]),
        "annotations": {**WRITES, "idempotentHint": False},
    }),
    "turo_changes": (tool_turo_changes, {
        "title": "Turo changes made",
        "description": (
            "Turo setting reads and changes, newest first: what was asked, its state, "
            "before and after, and whether it can be undone."
        ),
        "inputSchema": _schema({"limit": _LIMIT}),
    }),
    "file_toll_invoice": (tool_file_toll_invoice, {
        "title": "File a rental's tolls on Turo",
        "description": (
            "File the toll reimbursement for one rental on Turo, through the operator's "
            "browser extension, as the File button on the invoices page does. The ledger "
            "decides the amount and the crossings; it is refused if the ledger would not "
            "file it, and until the E-ZPass data reaches 3 days past the trip's end (late "
            "crossings), except in the window's last 7 days. A guest cannot be asked twice "
            "for one crossing. This asks a guest for money and cannot be taken back."
        ),
        "inputSchema": _schema({
            "trip_id": {"type": "string", "description": "From next_to_file."},
        }, ["trip_id"]),
        "annotations": {**WRITES, "destructiveHint": True, "openWorldHint": True},
    }),
    "propose_filing": (tool_propose_filing, {
        "title": "Propose filing a rental's tolls",
        "description": (
            "Put a rental's unfiled tolls in front of the operator to file on Turo. It is "
            "NOT filed: the operator presses File on the invoices page, and only then does "
            "the extension file it. Refused if the ledger would not file it. The note is "
            "shown beside the button."
        ),
        "inputSchema": _schema({
            "trip_id": {"type": "string", "description": "From next_to_file."},
            "note": {"type": "string", "description": "Why, in a sentence."},
        }, ["trip_id"]),
        "annotations": WRITES,
    }),
    "filing_proposals": (tool_filing_proposals, {
        "title": "Filing proposals",
        "description": (
            "Filings proposed, newest first, and what became of each: proposed (waiting), "
            "filed (File pressed; filing_state is the extension's progress), dismissed."
        ),
        "inputSchema": _schema({
            "state": {"type": "string", "enum": ["proposed", "filed", "dismissed"]},
            "trip_id": {"type": "string"},
            "limit": _LIMIT,
        }),
    }),
}

READ_ONLY = {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False}


def _tool_list() -> list[dict[str, Any]]:
    return [{"name": name, "annotations": READ_ONLY, **spec} for name, (_, spec) in TOOLS.items()]


# ---------------------------------------------------------------------------
# The transport
# ---------------------------------------------------------------------------

def _error(id_: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}


def _result(id_: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": id_, "result": result}


def _call(session: Session, auth: str, params: dict[str, Any]) -> dict[str, Any]:
    name = params.get("name")
    found = TOOLS.get(name) if isinstance(name, str) else None
    if found is None:
        raise KeyError(name)
    args = params.get("arguments") or {}
    if not isinstance(args, dict):
        return {"content": [{"type": "text", "text": "arguments must be an object"}],
                "isError": True}
    try:
        data = found[0](session, auth, args)
    except _ToolError as exc:
        return {"content": [{"type": "text", "text": str(exc)}], "isError": True}
    data["as_of"] = datetime.now(UTC).isoformat(timespec="seconds")
    return {
        "content": [{"type": "text", "text": json.dumps(data, separators=(",", ":"))}],
        "structuredContent": data,
        "isError": False,
    }


def _handle(session: Session, auth: str, message: Any) -> dict[str, Any] | None:
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
        return _error(None, -32600, "not a JSON-RPC 2.0 request")
    method = message.get("method")
    id_ = message.get("id")
    if "id" not in message:
        # A notification (initialized, cancelled): nothing to answer.
        return None
    params = message.get("params") or {}
    if not isinstance(params, dict):
        return _error(id_, -32602, "params must be an object")
    if method == "initialize":
        asked = params.get("protocolVersion")
        return _result(id_, {
            "protocolVersion": asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "turonomics", "title": "Turonomics", "version": "1.0.0"},
            "instructions": INSTRUCTIONS,
        })
    if method == "ping":
        return _result(id_, {})
    if method == "tools/list":
        return _result(id_, {"tools": _tool_list()})
    if method == "tools/call":
        try:
            return _result(id_, _call(session, auth, params))
        except KeyError:
            return _error(id_, -32602, f"no tool named {params.get('name')!r}")
    return _error(id_, -32601, f"method {method!r} is not offered here")


def _allowed_origin(origin: str | None) -> bool:
    """No Origin (a program, not a page) or one of the site's own.

    The transport asks servers to check it: a page in some other tab must not
    be able to drive this with a token it found.
    """
    if not origin:
        return True
    allowed = os.environ.get(
        "CORS_ALLOW_ORIGINS",
        "https://turonomics-site.onrender.com,http://localhost:8080,http://127.0.0.1:8080",
    )
    return origin in {o.strip() for o in allowed.split(",") if o.strip()}


def _refusal(status: int, message: str) -> JSONResponse:
    headers = {"WWW-Authenticate": 'Bearer realm="turonomics"'} if status == 401 else None
    return JSONResponse(_error(None, -32001, message), status_code=status, headers=headers)


@router.post("/mcp")
def mcp(
    session: DbSession,
    message: Annotated[Any, Body()] = None,
    authorization: Annotated[str | None, Header()] = None,
    origin: Annotated[str | None, Header()] = None,
) -> Response:
    # A plain function, not a coroutine: the tools call the same synchronous
    # database code the pages' routes do, and FastAPI runs this in its thread
    # pool rather than stalling every other request while a ledger is read.
    if not _allowed_origin(origin):
        return _refusal(403, "requests from that origin are not accepted")
    expected = os.environ.get("TOLLS_TOKEN", "").strip()
    if not expected:
        return _refusal(503, "the MCP endpoint is off until TOLLS_TOKEN is set on the API")
    supplied = (authorization or "").removeprefix("Bearer ").strip()
    if not supplied or not hmac.compare_digest(supplied, expected):
        return _refusal(401, "bad or missing token")
    if isinstance(message, list):
        # Batches were dropped from the protocol in 2025-06-18.
        return JSONResponse(_error(None, -32600, "send one request at a time"), status_code=400)
    answer = _handle(session, f"Bearer {supplied}", message)
    if answer is None:
        return Response(status_code=202)
    return JSONResponse(answer)


@router.get("/mcp")
def mcp_stream() -> Response:
    """No event stream is offered; the transport reads 405 as exactly that."""
    return Response(status_code=405, headers={"Allow": "POST"})
