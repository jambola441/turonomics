"""The API as an MCP server: the transport, the guard on it, and each tool.

The tools call the same route functions the pages do, so what is tested here
is what each one picks out and how it says it, and that nothing leaves this
endpoint without the token.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from turonomics_api.db.models import (
    ReimbursementInvoice,
    Toll,
    Trip,
    TripSource,
    TripState,
    Vehicle,
)
from turonomics_api.routers.mcp import PROTOCOL_VERSIONS

from .conftest import requires_db

TOKEN = "s3cret"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
NOW = datetime.now(UTC)


def rpc(client: Any, method: str, params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None, id_: int = 1) -> Any:
    body: dict[str, Any] = {"jsonrpc": "2.0", "id": id_, "method": method}
    if params is not None:
        body["params"] = params
    return client.post("/mcp", json=body, headers=AUTH if headers is None else headers)


def call(client: Any, name: str, **arguments: Any) -> dict[str, Any]:
    answer = rpc(client, "tools/call", {"name": name, "arguments": arguments}).json()
    result: dict[str, Any] = answer["result"]
    return result


def data(client: Any, name: str, **arguments: Any) -> dict[str, Any]:
    result = call(client, name, **arguments)
    assert result["isError"] is False, result
    parsed: dict[str, Any] = json.loads(result["content"][0]["text"])
    assert parsed == result["structuredContent"], "the text and the structure say the same"
    return parsed


@pytest.fixture()
def token(monkeypatch) -> None:
    monkeypatch.setenv("TOLLS_TOKEN", TOKEN)


@pytest.fixture()
def fleet(session) -> dict[str, Trip]:
    """Three rentals: one to file, one long settled, one expired."""
    car = Vehicle(nickname="Jolene", make="Toyota", model="Corolla", year=2025,
                  plate="LZA7293")
    session.add(car)
    session.flush()

    def rental(name: str, reservation: str, ended_days_ago: int, **extra: Any) -> Trip:
        ends = NOW - timedelta(days=ended_days_ago)
        trip = Trip(vehicle_id=car.id, turo_trip_id=reservation, guest_name=name,
                    starts_at=ends - timedelta(days=3), ends_at=ends,
                    state=TripState.completed, source=TripSource.extension, **extra)
        session.add(trip)
        session.flush()
        return trip

    soon = rental("Ana", "1001", 80)
    later = rental("Bo", "1002", 10, turo_detail={"secret": "x" * 50},
                   turo_messages=[{"text": "Keys are in the box", "authorDriverRole": "HOST",
                                   "author": {"firstName": "Host"}}])
    old = rental("Cy", "1003", 200)
    for i, (trip, cents) in enumerate([(soon, 746), (later, 911), (later, 300), (old, 500)]):
        session.add(Toll(fingerprint=f"f{i}", occurred_at=trip.starts_at + timedelta(hours=5),
                         plaza="RKB", amount_cents=cents, transponder_id="99900000111",
                         vehicle_id=car.id, trip_id=trip.id))
    # A crossing nobody can be billed for.
    session.add(Toll(fingerprint="loose", occurred_at=NOW - timedelta(days=5), plaza="VNB",
                     amount_cents=1234, transponder_id="99900000999"))
    session.commit()
    return {"soon": soon, "later": later, "old": old}


# ---------------------------------------------------------------------------
# The guard
# ---------------------------------------------------------------------------

@requires_db
def test_it_is_off_without_a_token_configured(api_client, monkeypatch) -> None:
    """The pages let everything through with TOLLS_TOKEN unset; this does not."""
    monkeypatch.delenv("TOLLS_TOKEN", raising=False)
    resp = rpc(api_client, "tools/list", headers={})
    assert resp.status_code == 503
    assert "TOLLS_TOKEN" in resp.json()["error"]["message"]


@requires_db
def test_a_wrong_or_missing_token_is_refused(api_client, token) -> None:
    for headers in ({}, {"Authorization": "Bearer nope"}):
        resp = rpc(api_client, "tools/list", headers=headers)
        assert resp.status_code == 401
        assert resp.headers["www-authenticate"].startswith("Bearer")


@requires_db
def test_a_page_on_another_origin_cannot_drive_it(api_client, token) -> None:
    resp = rpc(api_client, "tools/list", headers={**AUTH, "Origin": "https://evil.example"})
    assert resp.status_code == 403
    ok = rpc(api_client, "tools/list",
             headers={**AUTH, "Origin": "https://turonomics-site.onrender.com"})
    assert ok.status_code == 200


def test_there_is_no_event_stream(client) -> None:
    resp = client.get("/mcp")
    assert resp.status_code == 405 and resp.headers["allow"] == "POST"


# ---------------------------------------------------------------------------
# The protocol
# ---------------------------------------------------------------------------

@requires_db
def test_initialize_agrees_a_version_and_says_what_it_is(api_client, token) -> None:
    result = rpc(api_client, "initialize", {"protocolVersion": "2025-03-26",
                                            "capabilities": {}, "clientInfo": {"name": "t"}}
                 ).json()["result"]
    assert result["protocolVersion"] == "2025-03-26"
    assert result["capabilities"] == {"tools": {"listChanged": False}}
    assert result["serverInfo"]["name"] == "turonomics"
    assert "integer cents" in result["instructions"]
    newer = rpc(api_client, "initialize", {"protocolVersion": "2099-01-01"}).json()["result"]
    assert newer["protocolVersion"] == PROTOCOL_VERSIONS[0], "it offers its newest instead"


@requires_db
def test_a_notification_gets_no_answer(api_client, token) -> None:
    resp = api_client.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                           headers=AUTH)
    assert resp.status_code == 202 and resp.content == b""


@requires_db
def test_what_is_not_offered_is_said_so(api_client, token) -> None:
    assert rpc(api_client, "resources/list").json()["error"]["code"] == -32601
    unknown = rpc(api_client, "tools/call", {"name": "file_on_turo", "arguments": {}}).json()
    assert unknown["error"]["code"] == -32602
    batch = api_client.post("/mcp", json=[{"jsonrpc": "2.0", "id": 1, "method": "ping"}],
                            headers=AUTH)
    assert batch.status_code == 400
    assert rpc(api_client, "ping").json()["result"] == {}


@requires_db
def test_every_tool_is_listed_as_read_only(api_client, token) -> None:
    tools = rpc(api_client, "tools/list").json()["result"]["tools"]
    assert {t["name"] for t in tools} == {
        "next_to_file", "ledger", "find_trips", "trip", "tolls", "extension_status"}
    for tool in tools:
        assert tool["annotations"]["readOnlyHint"] is True
        assert tool["annotations"]["destructiveHint"] is False
        assert tool["inputSchema"]["type"] == "object" and tool["description"]


# ---------------------------------------------------------------------------
# The tools
# ---------------------------------------------------------------------------

@requires_db
def test_next_to_file_is_the_open_window_soonest_first(api_client, token, fleet) -> None:
    out = data(api_client, "next_to_file")
    names = [r["guest_name"] for r in out["rentals"]]
    assert names == ["Ana", "Bo"], "Cy's window closed; Ana's closes first"
    assert out["rentals"][0]["tolls_to_file_cents"] == 746
    assert out["rentals"][1]["tolls_to_file_cents"] == 1211
    assert out["rentals"][0]["days_left"] < out["rentals"][1]["days_left"]
    assert out["collectable_cents"] == 746 + 1211
    assert "as_of" in out
    assert data(api_client, "next_to_file", limit=1)["more"] == 1


@requires_db
def test_next_to_file_leaves_out_what_turo_was_already_asked_for(
    api_client, token, fleet, session
) -> None:
    session.add(ReimbursementInvoice(
        fingerprint="res:1001:746", reservation_id="1001", state="charged", total_cents=746,
        toll_cents=746, lines=[["Tolls", 746]], trip_id=fleet["soon"].id,
        last_seen_at=NOW, charged_at=NOW))
    for toll in session.query(Toll).filter(Toll.trip_id == fleet["soon"].id):
        toll.filed_at = NOW
    session.commit()
    assert [r["guest_name"] for r in data(api_client, "next_to_file")["rentals"]] == ["Bo"]


# Turo's odometer figures for a rental 300 miles over at $0.35 a mile.
OVER = {"odometerDetail": {
    "distanceDriven": {"scalar": 500}, "distanceLimit": {"scalar": 200},
    "excessDistance": {"scalar": 300},
    "distanceOverageFee": {"money": {"amount": 0.35}, "distance": {"scalar": 1}},
}}


@requires_db
def test_distance_counts_only_while_the_window_is_open(api_client, token, fleet, session) -> None:
    fleet["soon"].turo_detail = OVER
    fleet["old"].turo_detail = OVER
    session.commit()
    rentals = data(api_client, "next_to_file")["rentals"]
    assert [r["guest_name"] for r in rentals] == ["Ana", "Bo"], "Cy's distance expired with it"
    assert rentals[0]["distance_to_file_cents"] == 10500


@requires_db
def test_crossings_held_for_a_look_are_not_offered_to_file(
    api_client, token, fleet, session
) -> None:
    """Turo charged Bo's rental without saying what for: the ledger says check
    that invoice first, and the tool must not count Bo's crossings as fileable."""
    session.add(ReimbursementInvoice(
        fingerprint="res:1002:5000", reservation_id="1002", state="charged", total_cents=5000,
        lines=[], trip_id=fleet["later"].id, last_seen_at=NOW, charged_at=NOW))
    session.commit()
    assert data(api_client, "ledger", guest="bo")["rows"][0]["state"] == "check Turo's invoice"
    assert [r["guest_name"] for r in data(api_client, "next_to_file")["rentals"]] == ["Ana"]


@requires_db
def test_the_ledger_filters_by_state_and_guest(api_client, token, fleet) -> None:
    every = data(api_client, "ledger")
    assert {r["guest_name"] for r in every["rows"]} == {"Ana", "Bo", "Cy"}
    assert every["totals"]["tolls_cents"] == 746 + 911 + 300 + 500
    assert [r["guest_name"] for r in data(api_client, "ledger", state="expired")["rows"]] == ["Cy"]
    assert [r["guest_name"] for r in data(api_client, "ledger", guest="an")["rows"]] == ["Ana"]


@requires_db
def test_find_trips_by_name_reservation_and_dates(api_client, token, fleet) -> None:
    assert [t["guest_name"] for t in data(api_client, "find_trips", query="bo")["trips"]] == ["Bo"]
    assert [t["guest_name"] for t in data(api_client, "find_trips", query="1003")["trips"]] == ["Cy"]
    # A range the rental overlaps, not one it sits inside: Ana's rental ended
    # 80 days ago, so a range starting the day before it ended still finds it.
    ended = fleet["soon"].ends_at.date()
    out = data(api_client, "find_trips", **{"from": (ended - timedelta(days=1)).isoformat(),
                                            "to": (ended + timedelta(days=30)).isoformat()})
    assert [t["guest_name"] for t in out["trips"]] == ["Ana"]
    # And a range that ends while the rental is still under way.
    started = fleet["soon"].starts_at.date()
    out = data(api_client, "find_trips", **{"from": (started - timedelta(days=5)).isoformat(),
                                            "to": (started + timedelta(days=1)).isoformat()})
    assert [t["guest_name"] for t in out["trips"]] == ["Ana"]
    bad = call(api_client, "find_trips", **{"from": "last tuesday"})
    assert bad["isError"] is True and "YYYY-MM-DD" in bad["content"][0]["text"]


@requires_db
def test_one_trip_without_turos_raw_detail_or_the_thread_unless_asked(
    api_client, token, fleet
) -> None:
    trip_id = str(fleet["later"].id)
    out = data(api_client, "trip", trip_id=trip_id)
    assert out["trip"]["guest_name"] == "Bo"
    assert len(out["tolls"]) == 2
    assert "turo_detail" not in out and "messages" not in out
    assert out["message_count"] == 1
    full = data(api_client, "trip", trip_id=trip_id, include_messages=True)
    assert full["messages"][0]["text"] == "Keys are in the box"
    missing = call(api_client, "trip", trip_id="00000000-0000-0000-0000-000000000000")
    assert missing["isError"] is True and "no rental" in missing["content"][0]["text"]
    garbled = call(api_client, "trip", trip_id="Bo's trip")
    assert garbled["isError"] is True and "find_trips" in garbled["content"][0]["text"]


@requires_db
def test_tolls_open_nobodys_or_one_rentals(api_client, token, fleet, session) -> None:
    every = data(api_client, "tolls", filter="all")
    assert len(every["tolls"]) == 5
    loose = data(api_client, "tolls", filter="no_rental")["tolls"]
    assert [t["amount_cents"] for t in loose] == [1234]
    bos = data(api_client, "tolls", filter="all", trip_id=str(fleet["later"].id))["tolls"]
    assert sorted(t["amount_cents"] for t in bos) == [300, 911]
    for toll in session.query(Toll).filter(Toll.trip_id == fleet["later"].id):
        toll.recovered_at = NOW
    session.commit()
    assert len(data(api_client, "tolls")["tolls"]) == 3, "open is the default"
    wrong = call(api_client, "tolls", filter="unpaid")
    assert wrong["isError"] is True


@requires_db
def test_extension_status_says_whether_it_is_listening(api_client, token) -> None:
    out = data(api_client, "extension_status")
    assert out["listening"] is False and out["commands"] == []
