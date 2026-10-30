"""Turo's own settings, read and changed through the extension from the MCP endpoint.

The cases worth testing hard: a change only goes to an extension that knows
the operations; the values, which can hold a lockbox code, are never in the
public command list; the site cannot queue one; a change is answered with
before and after once the extension reports; and undo replays what the
extension said would put it back.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.orm import sessionmaker

from turonomics_api.db.models import ExtensionCheckin, ExtensionCommand, Vehicle

from .conftest import requires_db

pytestmark = requires_db

AUTH = {"Authorization": "Bearer s3cret"}
NEW = {**AUTH, "X-Extension-Version": "1.21.0"}
LISTING = "3218625"


@pytest.fixture(autouse=True)
def _token(monkeypatch):
    monkeypatch.setenv("TOLLS_TOKEN", "s3cret")
    # A claim marks the extension listening, and a tool would then wait the
    # full time for an answer no test sends. The round-trip test sets its own.
    monkeypatch.setattr("turonomics_api.routers.turo_changes.WAIT_SECONDS", 0.5)


@pytest.fixture()
def jolene(session) -> Vehicle:
    car = Vehicle(nickname="Jolene", make="Toyota", model="Corolla", year=2025,
                  turo_listing_id=LISTING)
    session.add(car)
    session.commit()
    return car


def _checked_in(session, version: str = "1.21.0", ago: timedelta = timedelta(hours=1)) -> None:
    session.merge(ExtensionCheckin(id=1, seen_at=datetime.now(UTC) - ago, version=version))
    session.commit()


def call(client: Any, name: str, **arguments: Any) -> dict[str, Any]:
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": name, "arguments": arguments}}
    result: dict[str, Any] = client.post("/mcp", json=body, headers=AUTH).json()["result"]
    return result


def data(client: Any, name: str, **arguments: Any) -> dict[str, Any]:
    result = call(client, name, **arguments)
    assert result["isError"] is False, result
    out: dict[str, Any] = json.loads(result["content"][0]["text"])
    return out


def test_nothing_is_queued_for_an_extension_that_cannot_run_it(api_client, session, jolene) -> None:
    missing = call(api_client, "read_turo_setting", setting="guest_instructions", car="Jolene")
    assert missing["isError"] and "no browser extension" in missing["content"][0]["text"]
    _checked_in(session, "1.20.0")
    old = call(api_client, "read_turo_setting", setting="guest_instructions", car="Jolene")
    assert old["isError"] and "1.21.0" in old["content"][0]["text"]
    assert session.query(ExtensionCommand).count() == 0


def test_a_change_names_the_operation_the_listing_and_the_values(
    api_client, session, jolene
) -> None:
    _checked_in(session)
    out = data(api_client, "change_turo_setting", setting="guest_instructions", car="jolene",
               values={"pickupReturnInstructions": "The lockbox code is 4321"})
    assert out["state"] == "queued" and out["can_undo"] is False
    command = session.query(ExtensionCommand).one()
    assert command.kind == "turo"
    assert command.payload == {"operation": "guest_instructions", "mode": "write",
                               "vehicle_id": LISTING,
                               "values": {"pickupReturnInstructions": "The lockbox code is 4321"}}


@pytest.mark.parametrize(("arguments", "said"), [
    ({"setting": "prices", "car": "Jolene", "values": {"x": 1}}, "setting is one of"),
    ({"setting": "guest_instructions", "car": "Nobody", "values": {"a": 1}}, "no car called"),
    ({"setting": "guest_instructions", "values": {"a": 1}}, "which car"),
    ({"setting": "guest_instructions", "car": "Jolene", "values": {}}, "nothing to change"),
])
def test_what_cannot_be_asked_for_is_refused_before_it_is_queued(
    api_client, session, jolene, arguments, said
) -> None:
    _checked_in(session)
    out = call(api_client, "change_turo_setting", **arguments)
    assert out["isError"] and said in out["content"][0]["text"]
    assert session.query(ExtensionCommand).count() == 0


def test_a_car_with_no_listing_id_is_refused(api_client, session) -> None:
    _checked_in(session)
    session.add(Vehicle(nickname="Jimmy", make="Toyota", model="4-Runner", year=2023))
    session.commit()
    out = call(api_client, "read_turo_setting", setting="extras", car="Jimmy")
    assert out["isError"] and "no Turo listing id" in out["content"][0]["text"]


def test_an_account_setting_needs_no_car(api_client, session) -> None:
    _checked_in(session)
    data(api_client, "read_turo_setting", setting="sms")
    payload = session.query(ExtensionCommand).one().payload
    assert payload == {"operation": "sms", "mode": "read", "values": {}}


def test_the_site_cannot_queue_one(api_client, session, jolene) -> None:
    out = api_client.post("/api/commands", headers=AUTH, json={"kind": "turo"})
    assert out.status_code == 422


def test_the_values_go_only_to_the_extension_that_claims_it(api_client, session, jolene) -> None:
    _checked_in(session)
    data(api_client, "change_turo_setting", setting="guest_instructions", car="Jolene",
         values={"pickupReturnInstructions": "The lockbox code is 4321"})
    public = api_client.get("/api/commands").text
    assert "4321" not in public and "guest_instructions" in public
    old = api_client.post("/api/commands/claim",
                          headers={**AUTH, "X-Extension-Version": "1.20.0"})
    assert old.status_code == 204, "an older extension would not know the operation"
    claimed = api_client.post("/api/commands/claim", headers=NEW).json()
    assert claimed["kind"] == "turo"
    assert claimed["payload"]["values"]["pickupReturnInstructions"].endswith("4321")


def _report(api_client, command_id: str, ok: bool = True) -> None:
    detail = {"before": {"welcomeMessage": "Hi"}, "after": {"welcomeMessage": "Welcome!"},
              "restore": {"operation": "guest_instructions",
                          "values": {"welcomeMessage": "Hi"}}}
    api_client.post(f"/api/commands/{command_id}/done", headers=NEW,
                    json={"ok": ok, "result": "guest_instructions changed", "detail": detail})


def test_the_report_is_kept_and_undo_replays_its_restore(api_client, session, jolene) -> None:
    _checked_in(session)
    change = data(api_client, "change_turo_setting", setting="guest_instructions",
                  car="Jolene", values={"welcomeMessage": "Welcome!"})
    claimed = api_client.post("/api/commands/claim", headers=NEW).json()
    _report(api_client, claimed["id"])

    logged = data(api_client, "turo_changes")["changes"][0]
    assert logged["change_id"] == change["change_id"] and logged["state"] == "done"
    assert logged["before"] == {"welcomeMessage": "Hi"}
    assert logged["after"] == {"welcomeMessage": "Welcome!"}
    assert logged["can_undo"] is True

    undo = data(api_client, "undo_turo_change", change_id=change["change_id"])
    assert undo["setting"] == "guest_instructions" and undo["values"] == {"welcomeMessage": "Hi"}
    redo = session.get(ExtensionCommand, undo["change_id"])
    assert redo.payload["vehicle_id"] == LISTING and redo.payload["undoes"] == change["change_id"]


def test_a_change_that_failed_or_a_read_cannot_be_undone(api_client, session, jolene) -> None:
    _checked_in(session)
    change = data(api_client, "change_turo_setting", setting="guest_instructions",
                  car="Jolene", values={"welcomeMessage": "Welcome!"})
    claimed = api_client.post("/api/commands/claim", headers=NEW).json()
    _report(api_client, claimed["id"], ok=False)
    failed = call(api_client, "undo_turo_change", change_id=change["change_id"])
    assert failed["isError"] and "cannot be undone" in failed["content"][0]["text"]

    read = data(api_client, "read_turo_setting", setting="sms")
    refused = call(api_client, "undo_turo_change", change_id=read["change_id"])
    assert refused["isError"]


def test_the_tool_waits_for_a_listening_extension_and_answers_with_its_report(
    api_client, session, engine, jolene, monkeypatch
) -> None:
    """The whole round trip in one call, as Claude sees it."""
    monkeypatch.setattr("turonomics_api.routers.turo_changes.WAIT_SECONDS", 15.0)
    _checked_in(session, ago=timedelta(seconds=5))
    maker = sessionmaker(bind=engine, future=True)

    def extension() -> None:
        # Another connection, as the extension's claim and report would be.
        for _ in range(40):
            time.sleep(0.25)
            with maker() as other:
                command = other.query(ExtensionCommand).filter_by(state="queued").first()
                if command is None:
                    continue
                command.state = "done"
                command.finished_at = datetime.now(UTC)
                command.result = "read sms"
                command.detail = {"current": {"sms": True}}
                other.commit()
                return

    worker = threading.Thread(target=extension)
    worker.start()
    out = data(api_client, "read_turo_setting", setting="sms")
    worker.join()
    assert out["state"] == "done" and out["current"] == {"sms": True}
