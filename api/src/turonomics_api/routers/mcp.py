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

It reads, and it writes one thing: a draft message to a rental's guest, which
nothing sends until the operator presses Send on it on the site. There is no
tool to approve or send one. Filing on Turo, ticking off crossings and the
rest stay on the site and the extension, where a person presses the button.

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
from datetime import UTC, date, datetime
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
    "draft and is waiting for them. Nothing else here writes: filing, ticking off and "
    "pulling from Turo are done on the site or by the browser extension."
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


def _schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "object", "properties": properties,
                              "additionalProperties": False}
    if required:
        schema["required"] = required
    return schema


_LIMIT = {"type": "integer", "minimum": 1, "description": "At most this many rows."}

Tool = Callable[[Session, str, dict[str, Any]], dict[str, Any]]

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
        "annotations": {"readOnlyHint": False, "destructiveHint": False,
                        "idempotentHint": False, "openWorldHint": False},
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
