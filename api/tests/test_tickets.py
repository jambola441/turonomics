"""NYC parking and camera tickets: read by plate, kept by summons, billed to rentals.

Rows are shaped as the city's "Open Parking and Camera Violations" data returns
them (dates MM/DD/YYYY, times "06:01P", dollars as text). What matters most:
the right rental is billed and no other, a ticket is updated rather than
doubled when the city changes it, a ticket with no time is not pinned on a
guest who had the car for only part of that day, and the guest is asked for
the fine, not for the host's late-payment penalty.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from turonomics_api.db.models import Ticket, Trip, TripSource, TripState, Vehicle
from turonomics_api.ingest import tickets as tickets_mod
from turonomics_api.ingest.tickets import (
    billable_cents,
    fetch_tickets,
    issued_at,
    kind_of,
    parse,
    store,
    sync_tickets,
)
from turonomics_api.settings import fleet_timezone

from .conftest import requires_db

NY = fleet_timezone()
PLATE = "LWH4685"
AUTH = {"Authorization": "Bearer s3cret"}


def row(summons: str = "5155965779", *, plate: str = PLATE, day: str = "09/08/2026",
        at: str | None = "06:01P", violation: str = "FAILURE TO STOP AT RED LIGHT",
        fine: str = "50", penalty: str = "0", paid: str = "0", due: str = "50",
        **extra: Any) -> dict[str, Any]:
    out: dict[str, Any] = {
        "plate": plate, "state": "NY", "license_type": "PAS", "summons_number": summons,
        "issue_date": day, "violation": violation, "fine_amount": fine,
        "penalty_amount": penalty, "interest_amount": "0", "reduction_amount": "0",
        "payment_amount": paid, "amount_due": due, "precinct": "000", "county": "BK",
        "issuing_agency": "DEPARTMENT OF TRANSPORTATION",
        "summons_image": {"url": "http://nycserv.nyc.gov/NYCServWeb/ShowImage?searchID=X",
                          "description": "View Summons"},
        **extra,
    }
    if at is not None:
        out["violation_time"] = at
    return out


# ---------------------------------------------------------------------------
# Reading the city's rows
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(("violation", "kind"), [
    ("PHTO SCHOOL ZN SPEED VIOLATION", "speed_camera"),
    ("FAILURE TO STOP AT RED LIGHT", "red_light_camera"),
    ("BUS LANE VIOLATION", "bus_lane_camera"),
    ("MTA CAMERA VIOLATION - DOUBLE PARKING", "bus_lane_camera"),
    ("NO PARKING-STREET CLEANING", "parking"),
    ("FIRE HYDRANT", "parking"),
])
def test_the_kind_comes_from_the_citys_wording(violation, kind) -> None:
    assert kind_of(violation) == kind


@pytest.mark.parametrize(("time_text", "hour", "minute"), [
    ("06:01P", 18, 1), ("09:02A", 9, 2), ("12:54P", 12, 54), ("12:30A", 0, 30), ("0831A", 8, 31),
])
def test_the_time_is_new_york_time(time_text, hour, minute) -> None:
    when, known = issued_at("09/08/2026", time_text)
    assert known
    assert when == datetime(2026, 9, 8, hour, minute, tzinfo=NY)


@pytest.mark.parametrize("time_text", [None, "", "99:99A", "noon"])
def test_a_ticket_with_no_usable_time_is_the_start_of_its_day_not_known(time_text) -> None:
    when, known = issued_at("09/08/2026", time_text)
    assert not known and when == datetime(2026, 9, 8, tzinfo=NY)


def test_a_row_is_read_into_cents_and_its_picture() -> None:
    parsed = parse(row(fine="115", paid="65.50", due="49.50"))
    assert parsed is not None
    assert (parsed.fine_cents, parsed.paid_cents, parsed.due_cents) == (11500, 6550, 4950)
    assert parsed.image_url and "ShowImage" in parsed.image_url
    assert parsed.plate == PLATE and parsed.plate_state == "NY"


def test_a_row_without_a_summons_or_date_is_skipped() -> None:
    assert parse(row(summons="")) is None
    assert parse({**row(), "issue_date": None}) is None
    assert parse({**row(), "issue_date": "not a date"}) is None


def test_the_city_is_asked_for_the_fleets_plates_in_pages() -> None:
    asked: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        asked.append(params)
        page = [row(str(i)) for i in range(5000)] if params["$offset"] == "0" else [row("x")]
        return httpx.Response(200, json=page)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    rows = fetch_tickets([PLATE, "ABC1234"], http=client)
    assert len(rows) == 5001 and len(asked) == 2
    assert asked[0]["$where"] == f"plate in ('{PLATE}','ABC1234')"


# ---------------------------------------------------------------------------
# Keeping them, and whose they are
# ---------------------------------------------------------------------------

@pytest.fixture()
def jolene(session) -> Vehicle:
    car = Vehicle(nickname="Jolene", make="Toyota", model="Corolla", year=2025, plate=PLATE)
    session.add(car)
    session.commit()
    return car


def rental(session, car, starts: datetime, ends: datetime, **extra: Any) -> Trip:
    trip = Trip(vehicle_id=car.id, guest_name=extra.pop("guest", "Daniela"), starts_at=starts,
                ends_at=ends, state=extra.pop("state", TripState.completed),
                source=TripSource.manual, **extra)
    session.add(trip)
    session.commit()
    return trip


SEP8 = datetime(2026, 9, 8, tzinfo=NY)


@requires_db
def test_a_ticket_is_billed_to_the_rental_that_had_the_car(session, jolene) -> None:
    trip = rental(session, jolene, SEP8 + timedelta(hours=10), SEP8 + timedelta(hours=20))
    other = Vehicle(nickname="Jerry", make="Toyota", model="Corolla", year=2025, plate="XYZ1")
    session.add(other)
    session.commit()
    rental(session, other, SEP8, SEP8 + timedelta(days=1), guest="Pablo")

    report = store(session, [row()])
    ticket = session.query(Ticket).one()
    assert report.new == 1 and report.billed == 1
    assert ticket.vehicle_id == jolene.id and ticket.trip_id == trip.id, "this car's rental"
    assert ticket.kind == "red_light_camera"


@requires_db
def test_a_ticket_outside_every_rental_is_left_unbilled(session, jolene) -> None:
    rental(session, jolene, SEP8 + timedelta(days=2), SEP8 + timedelta(days=3))
    store(session, [row()])
    assert session.query(Ticket).one().trip_id is None


@requires_db
def test_a_cancelled_rental_is_not_billed(session, jolene) -> None:
    rental(session, jolene, SEP8, SEP8 + timedelta(days=1), state=TripState.cancelled)
    store(session, [row()])
    assert session.query(Ticket).one().trip_id is None


@requires_db
def test_a_ticket_on_a_plate_not_in_the_fleet_is_kept_with_no_car(session, jolene) -> None:
    store(session, [row(plate="NOTOURS")])
    ticket = session.query(Ticket).one()
    assert ticket.vehicle_id is None and ticket.trip_id is None


@requires_db
def test_the_city_changing_a_ticket_updates_it_rather_than_doubling_it(session, jolene) -> None:
    store(session, [row(due="50")], now=datetime(2026, 9, 20, tzinfo=UTC))
    later = datetime(2026, 10, 20, tzinfo=UTC)
    report = store(session, [row(penalty="10", due="60")], now=later)
    ticket = session.query(Ticket).one()
    assert report.new == 0 and report.updated == 1
    assert ticket.due_cents == 6000 and ticket.penalty_cents == 1000
    assert ticket.last_seen_at == later and ticket.first_seen_at < later
    assert store(session, [row(penalty="10", due="60")]).updated == 0, "nothing changed"


@requires_db
def test_a_ticket_with_no_time_goes_only_to_a_rental_that_had_the_car_all_day(
    session, jolene
) -> None:
    part = rental(session, jolene, SEP8 + timedelta(hours=10), SEP8 + timedelta(hours=20))
    store(session, [row(at=None)])
    assert session.query(Ticket).one().trip_id is None, "the host may have had it that morning"

    session.delete(part)
    session.commit()
    whole = rental(session, jolene, SEP8 - timedelta(days=1), SEP8 + timedelta(days=2))
    store(session, [row(at=None)])
    assert session.query(Ticket).one().trip_id == whole.id


@requires_db
def test_a_rental_typed_in_later_picks_up_its_tickets(api_client, session, jolene) -> None:
    store(session, [row()])
    assert session.query(Ticket).one().trip_id is None
    out = api_client.post("/api/trips", json={
        "vehicle": "Jolene", "guest_name": "Serendipity",
        "starts_at": "2026-09-08T09:00", "ends_at": "2026-09-09T09:00"})
    assert out.status_code == 200, out.text
    session.expire_all()
    assert session.query(Ticket).one().trip_id is not None


def test_the_guest_is_asked_for_the_fine_not_the_hosts_late_penalty() -> None:
    ticket = Ticket(fine_cents=6500, penalty_cents=1000, interest_cents=150,
                    reduction_cents=500)
    assert billable_cents(ticket) == 6000


# ---------------------------------------------------------------------------
# The scheduled read
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _fresh_schedule(monkeypatch):
    monkeypatch.setattr(tickets_mod, "_last_sync", None)


@requires_db
def test_the_read_is_every_few_hours_not_every_poll(session, jolene) -> None:
    asked: list[list[str]] = []

    def fetch(plates, **kw):
        asked.append(plates)
        return [row()]

    t0 = datetime(2026, 11, 13, 12, tzinfo=UTC)
    assert sync_tickets(session, now=t0, fetch=fetch) is not None
    assert asked == [[PLATE]], "the fleet's plates"
    assert sync_tickets(session, now=t0 + timedelta(hours=1), fetch=fetch) is None
    assert sync_tickets(session, now=t0 + timedelta(hours=7), fetch=fetch) is not None
    assert sync_tickets(session, now=t0 + timedelta(hours=8), fetch=fetch, force=True)
    assert len(asked) == 3


@requires_db
def test_a_city_outage_does_not_break_the_poll(session, jolene) -> None:
    def down(plates, **kw):
        raise httpx.ConnectError("the city is down")

    assert sync_tickets(session, fetch=down) is None
    assert sync_tickets(session, fetch=lambda p, **k: [row()]) is not None, (
        "and it is tried again, not marked done")


# ---------------------------------------------------------------------------
# The API and the MCP tool
# ---------------------------------------------------------------------------

@requires_db
def test_the_list_is_behind_the_token_and_totals_whose_money_is_whose(
    api_client, session, jolene, monkeypatch
) -> None:
    monkeypatch.setenv("TOLLS_TOKEN", "s3cret")
    rental(session, jolene, SEP8, SEP8 + timedelta(days=1))
    store(session, [
        row("1", fine="50", due="0", paid="50"),
        row("2", day="10/01/2026", fine="65", penalty="10", due="75"),
    ])
    assert api_client.get("/api/tickets").status_code == 401
    data = api_client.get("/api/tickets", headers=AUTH).json()
    assert data["due_cents"] == 7500, "the city: penalty included"
    assert data["to_recover_cents"] == 5000, "the guest: the fine on their rental"
    assert data["unattributed_cents"] == 6500, "the October one no rental claims"
    billed = next(t for t in data["tickets"] if t["summons_number"] == "1")
    assert billed["guest_name"] == "Daniela" and billed["vehicle_nickname"] == "Jolene"

    done = api_client.post(f"/api/tickets/{billed['id']}/recovered", headers=AUTH).json()
    assert done["recovered_at"] is not None
    assert api_client.get("/api/tickets", headers=AUTH).json()["to_recover_cents"] == 0
    back = api_client.post(f"/api/tickets/{billed['id']}/recovered?undo=true", headers=AUTH)
    assert back.json()["recovered_at"] is None


@requires_db
def test_claude_reads_them_by_whose_they_are(api_client, session, jolene, monkeypatch) -> None:
    import json

    monkeypatch.setenv("TOLLS_TOKEN", "s3cret")
    trip = rental(session, jolene, SEP8, SEP8 + timedelta(days=1))
    store(session, [row("1"), row("2", day="10/01/2026")])

    def call(**arguments):
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "tickets", "arguments": arguments}}
        result = api_client.post("/mcp", json=body, headers=AUTH).json()["result"]
        assert result["isError"] is False, result
        return json.loads(result["content"][0]["text"])

    assert [t["summons_number"] for t in call()["tickets"]] == ["1"], "open: on a rental"
    assert [t["summons_number"] for t in call(filter="unattributed")["tickets"]] == ["2"]
    assert [t["summons_number"] for t in call(trip_id=str(trip.id), filter="all")["tickets"]] == ["1"]
