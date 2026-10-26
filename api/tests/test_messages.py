"""Messages to a guest: drafted, approved by a person, sent by the extension.

The cases worth testing hard are the ones where a message could reach a guest
without a person having pressed Send, or reach them twice: the MCP endpoint
approving one, a command queued without a draft, a double click, an old
extension that would fail it, and a send that was claimed and never answered.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from datetime import timedelta as td

import pytest

from turonomics_api.db.models import (
    ExtensionCheckin,
    ExtensionCommand,
    MessageDraft,
    Trip,
    TripSource,
    TripState,
    Vehicle,
)
from turonomics_api.routers import commands as commands_router

from .conftest import requires_db

NOW = datetime.now(UTC)
NEW = {"X-Extension-Version": "1.20.0"}


@pytest.fixture(autouse=True)
def _open(monkeypatch):
    monkeypatch.delenv("TOLLS_TOKEN", raising=False)


@pytest.fixture()
def car(session):
    vehicle = Vehicle(nickname="Jimmy", make="Toyota", model="Corolla", year=2024,
                      plate="LWH4685")
    session.add(vehicle)
    session.flush()
    return vehicle


@pytest.fixture()
def rental(session, car):
    trip = Trip(vehicle_id=car.id, turo_trip_id="59077848", guest_name="Austin",
                starts_at=NOW - td(days=3), ends_at=NOW - td(days=1),
                state=TripState.completed, source=TripSource.email)
    session.add(trip)
    session.commit()
    return trip


@pytest.fixture()
def typed_in(session, car):
    trip = Trip(vehicle_id=car.id, guest_name="Priya", starts_at=NOW - td(days=3),
                ends_at=NOW - td(days=1), state=TripState.completed,
                source=TripSource.manual)
    session.add(trip)
    session.commit()
    return trip


def _draft(api_client, trip, body="Hi Austin, the keys are in the lockbox.") -> dict:
    out = api_client.post("/api/messages", json={"trip_id": str(trip.id), "body": body})
    assert out.status_code == 200, out.text
    return out.json()


def _checked_in(session, version="1.20.0") -> None:
    session.merge(ExtensionCheckin(id=1, seen_at=NOW, version=version))
    session.commit()


# ---------------------------------------------------------------------------
# Drafts
# ---------------------------------------------------------------------------

@requires_db
def test_a_draft_is_kept_and_nothing_is_queued(api_client, session, rental) -> None:
    draft = _draft(api_client, rental, "  Hi Austin!  ")
    assert draft["state"] == "draft" and draft["body"] == "Hi Austin!"
    assert draft["created_by"] == "site" and draft["guest_name"] == "Austin"
    assert session.query(ExtensionCommand).count() == 0, "writing a draft sends nothing"
    listed = api_client.get(f"/api/messages?trip_id={rental.id}").json()["drafts"]
    assert [d["id"] for d in listed] == [draft["id"]]


@requires_db
def test_a_draft_needs_words_a_turo_thread_and_a_sane_length(
    api_client, rental, typed_in
) -> None:
    def post(trip, body):
        return api_client.post("/api/messages", json={"trip_id": str(trip.id), "body": body})

    assert post(rental, "   ").status_code == 422
    assert "2000" in post(rental, "x" * 2001).json()["detail"]
    assert "off-platform" in post(typed_in, "hello").json()["detail"]
    assert post(rental, "x" * 2000).status_code == 200


@requires_db
def test_only_a_draft_can_be_edited_or_discarded(api_client, rental) -> None:
    draft = _draft(api_client, rental)
    edited = api_client.post(f"/api/messages/{draft['id']}/edit", json={"body": "Hi again"})
    assert edited.json()["body"] == "Hi again"
    gone = api_client.post(f"/api/messages/{draft['id']}/discard").json()
    assert gone["state"] == "discarded"
    assert api_client.post(f"/api/messages/{draft['id']}/edit",
                           json={"body": "x"}).status_code == 409
    assert api_client.post(f"/api/messages/{draft['id']}/send").status_code == 409


@requires_db
def test_drafts_are_behind_the_token(api_client, monkeypatch, rental) -> None:
    monkeypatch.setenv("TOLLS_TOKEN", "s3cret")
    assert api_client.get("/api/messages").status_code == 401
    assert api_client.post("/api/messages", json={"trip_id": str(rental.id),
                                                  "body": "hi"}).status_code == 401


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------

@requires_db
def test_send_waits_for_an_extension_that_can_send(api_client, session, rental) -> None:
    draft = _draft(api_client, rental)
    refused = api_client.post(f"/api/messages/{draft['id']}/send")
    assert refused.status_code == 409 and "1.20.0" in refused.json()["detail"]
    _checked_in(session, "1.19.0")
    refused = api_client.post(f"/api/messages/{draft['id']}/send")
    assert refused.status_code == 409 and "1.19.0" in refused.json()["detail"]
    assert session.query(ExtensionCommand).count() == 0
    assert api_client.get("/api/messages").json()["drafts"][0]["state"] == "draft"


@requires_db
def test_send_queues_it_once_however_often_it_is_pressed(api_client, session, rental) -> None:
    _checked_in(session)
    draft = _draft(api_client, rental)
    first = api_client.post(f"/api/messages/{draft['id']}/send").json()
    again = api_client.post(f"/api/messages/{draft['id']}/send").json()
    assert first["state"] == again["state"] == "approved"
    commands = session.query(ExtensionCommand).all()
    assert len(commands) == 1 and commands[0].kind == "message"
    assert commands[0].trip_id == rental.id


@requires_db
def test_a_message_cannot_be_queued_without_a_draft(api_client, rental) -> None:
    out = api_client.post("/api/commands", json={"kind": "message", "trip_id": str(rental.id)})
    assert out.status_code == 422 and "draft" in out.json()["detail"]


@requires_db
def test_an_old_extension_is_never_handed_a_message(api_client, session, rental) -> None:
    _checked_in(session)
    draft = _draft(api_client, rental)
    api_client.post(f"/api/messages/{draft['id']}/send")
    old = api_client.post("/api/commands/claim", headers={"X-Extension-Version": "1.19.0"})
    assert old.status_code == 204, "an old extension would fail it"
    new = api_client.post("/api/commands/claim", headers=NEW)
    assert new.status_code == 200
    claimed = new.json()
    assert claimed["kind"] == "message" and claimed["turo_trip_id"] == "59077848"
    assert claimed["message"] == "Hi Austin, the keys are in the lockbox."


@requires_db
def test_the_text_is_not_in_the_public_command_list(api_client, session, rental) -> None:
    _checked_in(session)
    draft = _draft(api_client, rental, "the lockbox code is 4321")
    api_client.post(f"/api/messages/{draft['id']}/send")
    assert "4321" not in api_client.get("/api/commands").text


@requires_db
def test_the_extensions_report_lands_on_the_draft(api_client, session, rental) -> None:
    _checked_in(session)
    for ok, state in ((True, "sent"), (False, "failed")):
        draft = _draft(api_client, rental)
        api_client.post(f"/api/messages/{draft['id']}/send")
        command = api_client.post("/api/commands/claim", headers=NEW).json()
        api_client.post(f"/api/commands/{command['id']}/done",
                        json={"ok": ok, "result": "posted" if ok else "Turo said no"})
        after = next(d for d in api_client.get("/api/messages").json()["drafts"]
                     if d["id"] == draft["id"])
        assert after["state"] == state
        assert (after["sent_at"] is not None) is ok
        assert after["result"] == ("posted" if ok else "Turo said no")


@requires_db
def test_a_send_never_answered_is_failed_not_resent(api_client, session, rental) -> None:
    _checked_in(session)
    draft = _draft(api_client, rental)
    api_client.post(f"/api/messages/{draft['id']}/send")
    command = api_client.post("/api/commands/claim", headers=NEW).json()
    row = session.get(ExtensionCommand, command["id"])
    row.claimed_at = NOW - commands_router.STALE_AFTER - td(minutes=1)
    session.commit()
    api_client.get("/api/commands")
    after = api_client.get("/api/messages").json()["drafts"][0]
    assert after["state"] == "failed" and "check the thread" in after["result"]
    assert api_client.post("/api/commands/claim", headers=NEW).status_code == 204, (
        "and it is not handed out again")


# ---------------------------------------------------------------------------
# Through the MCP endpoint
# ---------------------------------------------------------------------------

def _mcp(api_client, name, **arguments):
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": name, "arguments": arguments}}
    return api_client.post("/mcp", json=body,
                           headers={"Authorization": "Bearer s3cret"}).json()["result"]


@requires_db
def test_claude_can_draft_but_not_send(api_client, monkeypatch, session, rental) -> None:
    monkeypatch.setenv("TOLLS_TOKEN", "s3cret")
    _checked_in(session)
    result = _mcp(api_client, "draft_message", trip_id=str(rental.id),
                  text="Hi Austin, just checking in.")
    assert result["isError"] is False
    out = json.loads(result["content"][0]["text"])
    assert out["state"] == "draft" and out["created_by"] == "mcp" and out["sent"] is False
    assert out["review_at"].endswith(f"/trips/#{rental.id}")
    assert session.query(ExtensionCommand).count() == 0, "nothing is queued by drafting"
    assert session.query(MessageDraft).one().state == "draft"

    tools = api_client.post("/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                            headers={"Authorization": "Bearer s3cret"}).json()["result"]["tools"]
    names = {t["name"] for t in tools}
    assert not {"send_message", "approve_message"} & names, "no tool approves"
    drafting = next(t for t in tools if t["name"] == "draft_message")
    assert drafting["annotations"]["readOnlyHint"] is False
    assert all(t["annotations"]["readOnlyHint"] for t in tools if t["name"] != "draft_message")

    listed = json.loads(_mcp(api_client, "message_drafts", state="draft")["content"][0]["text"])
    assert [d["body"] for d in listed["drafts"]] == ["Hi Austin, just checking in."]


@requires_db
def test_claudes_draft_is_refused_where_a_persons_would_be(
    api_client, monkeypatch, typed_in
) -> None:
    monkeypatch.setenv("TOLLS_TOKEN", "s3cret")
    result = _mcp(api_client, "draft_message", trip_id=str(typed_in.id), text="hello")
    assert result["isError"] is True and "off-platform" in result["content"][0]["text"]
    empty = _mcp(api_client, "draft_message", trip_id=str(typed_in.id), text=5)
    assert empty["isError"] is True


@requires_db
def test_a_trip_removed_takes_its_drafts(api_client, session, rental) -> None:
    _draft(api_client, rental)
    session.delete(session.get(Trip, rental.id))
    session.commit()
    assert session.query(MessageDraft).count() == 0
