"""Rentals arranged outside Turo.

A Turo rental arrives by email and nothing has to be typed. A rental arranged
directly leaves no trace, which meant its tolls could not be attributed to
anybody — the crossing was real, the guest was real, and the ledger reported
money nobody owed.

The point of typing one in is that the crossings inside it stop being a gap, so
most of these tests are about that happening, and about the ways it could
happen wrongly: the wrong car, the wrong zone, or a window that quietly
reassigns somebody else's toll.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from turonomics_api.db.models import Toll, Trip, TripSource, TripState, Vehicle

from .conftest import requires_db

pytestmark = requires_db

EASTERN = ZoneInfo("America/New_York")
HEADER = "Lane Txn ID,Tag/Plate #,Agency,Entry Plaza,Exit Plaza,Class,Date,Exit Time,Amount"


@pytest.fixture()
def jerry(session):
    car = Vehicle(nickname="Jerry", make="Toyota", model="Corolla", year=2025,
                  plate="LZA7293")
    session.add(car)
    session.flush()
    return car


def _statement(time: str = "02:00:00 PM", txn: str = "900") -> bytes:
    return (
        HEADER + "\n"
        f'{txn},NY LZA7293,MTAB&T,,RKB,31,10/04/2026,{time},$-9.11\n'
    ).encode()


def _body(**over: object) -> dict:
    body = {
        "vehicle": "Jerry",
        "guest_name": "Priya",
        # No zone, as a datetime-local input sends it.
        "starts_at": "2026-10-04T13:00:00",
        "ends_at": "2026-10-04T18:00:00",
    }
    body.update(over)
    return body


# ---------------------------------------------------------------------------
# The point of it
# ---------------------------------------------------------------------------
def test_recording_a_rental_attributes_the_crossings_inside_it(
    api_client, monkeypatch, session, jerry
):
    monkeypatch.delenv("TOLLS_TOKEN", raising=False)
    api_client.post("/api/tolls/import",
                    files={"statement": ("a.csv", _statement(), "text/csv")})
    assert api_client.get("/api/tolls").json()["unattributed_cents"] == 911

    created = api_client.post("/api/trips", json=_body())
    assert created.status_code == 200, created.text
    assert created.json()["tolls_matched"] == 1

    body = api_client.get("/api/tolls").json()
    assert body["tolls"][0]["guest_name"] == "Priya"
    assert body["unattributed_cents"] == 0


def test_the_times_are_read_as_fleet_local(api_client, monkeypatch, session, jerry):
    """A bare "2026-10-04T13:00" is 1pm where the cars are.

    Read as UTC it would be 9am Eastern, and a 2pm crossing would fall outside
    a window that is supposed to contain it. That is the same mistake that put
    every scraped toll four hours early, against the same consequence.
    """
    monkeypatch.delenv("TOLLS_TOKEN", raising=False)
    api_client.post("/api/trips", json=_body())
    trip = session.scalars(select(Trip)).one()
    assert trip.starts_at == datetime(2026, 10, 4, 13, 0, tzinfo=EASTERN)
    assert trip.starts_at.astimezone(UTC).hour == 17


def test_an_explicit_zone_is_respected(api_client, monkeypatch, session, jerry):
    monkeypatch.delenv("TOLLS_TOKEN", raising=False)
    api_client.post("/api/trips", json=_body(starts_at="2026-10-04T17:00:00+00:00",
                                             ends_at="2026-10-04T22:00:00+00:00"))
    trip = session.scalars(select(Trip)).one()
    assert trip.starts_at == datetime(2026, 10, 4, 13, 0, tzinfo=EASTERN)


def test_a_crossing_outside_the_window_is_left_alone(
    api_client, monkeypatch, session, jerry
):
    """So the test above cannot pass by sweeping up every loose crossing."""
    monkeypatch.delenv("TOLLS_TOKEN", raising=False)
    api_client.post("/api/tolls/import",
                    files={"statement": ("a.csv", _statement(time="11:00:00 PM"), "text/csv")})
    created = api_client.post("/api/trips", json=_body())
    assert created.json()["tolls_matched"] == 0
    assert api_client.get("/api/tolls").json()["tolls"][0]["guest_name"] is None


# ---------------------------------------------------------------------------
# Getting it wrong
# ---------------------------------------------------------------------------
def test_an_unknown_car_is_refused_and_names_the_known_ones(
    api_client, monkeypatch, session, jerry
):
    monkeypatch.delenv("TOLLS_TOKEN", raising=False)
    response = api_client.post("/api/trips", json=_body(vehicle="Herbie"))
    assert response.status_code == 422
    assert "Jerry" in response.json()["detail"]


def test_a_car_can_be_named_by_plate(api_client, monkeypatch, session, jerry):
    monkeypatch.delenv("TOLLS_TOKEN", raising=False)
    assert api_client.post("/api/trips", json=_body(vehicle="lza 7293")).status_code == 200
    assert session.scalars(select(Trip)).one().vehicle_id == jerry.id


def test_a_backwards_window_is_refused_with_a_sentence(
    api_client, monkeypatch, session, jerry
):
    """The table's own constraint would also stop it, with a stack trace."""
    monkeypatch.delenv("TOLLS_TOKEN", raising=False)
    response = api_client.post(
        "/api/trips", json=_body(starts_at="2026-10-04T18:00:00",
                                 ends_at="2026-10-04T13:00:00")
    )
    assert response.status_code == 422
    assert "end after it starts" in response.json()["detail"]
    assert session.scalar(select(Trip)) is None


def test_recording_a_rental_needs_the_token(api_client, monkeypatch, session, jerry):
    monkeypatch.setenv("TOLLS_TOKEN", "letmein")
    assert api_client.post("/api/trips", json=_body()).status_code == 401
    assert session.scalar(select(Trip)) is None
    assert api_client.post("/api/trips", json=_body(),
                           headers={"Authorization": "Bearer letmein"}).status_code == 200


# ---------------------------------------------------------------------------
# Undoing it
# ---------------------------------------------------------------------------
def test_deleting_a_rental_releases_its_crossings_without_deleting_them(
    api_client, monkeypatch, session, jerry
):
    """The toll still happened. Only the claim about who was driving goes."""
    monkeypatch.delenv("TOLLS_TOKEN", raising=False)
    api_client.post("/api/tolls/import",
                    files={"statement": ("a.csv", _statement(), "text/csv")})
    trip_id = api_client.post("/api/trips", json=_body()).json()["trip"]["id"]

    out = api_client.delete(f"/api/trips/{trip_id}").json()
    assert out == {"deleted": 1, "tolls_released": 1}

    body = api_client.get("/api/tolls").json()
    assert len(body["tolls"]) == 1
    assert body["tolls"][0]["guest_name"] is None
    assert body["unattributed_cents"] == 911


def test_a_turo_rental_cannot_be_deleted_here(api_client, monkeypatch, session, jerry):
    """It is a record of what happened, rebuilt from mail — the next sync would
    undo the deletion anyway."""
    monkeypatch.delenv("TOLLS_TOKEN", raising=False)
    trip = Trip(
        vehicle_id=jerry.id, guest_name="Dylan",
        starts_at=datetime(2026, 10, 4, 7, 0, tzinfo=EASTERN),
        ends_at=datetime(2026, 10, 4, 9, 0, tzinfo=EASTERN),
        state=TripState.completed, source=TripSource.email,
    )
    session.add(trip)
    session.flush()
    response = api_client.delete(f"/api/trips/{trip.id}")
    assert response.status_code == 422
    assert session.get(Trip, trip.id) is not None


def test_deleting_a_rental_twice_is_not_an_error(api_client, monkeypatch, session, jerry):
    monkeypatch.delenv("TOLLS_TOKEN", raising=False)
    trip_id = api_client.post("/api/trips", json=_body()).json()["trip"]["id"]
    assert api_client.delete(f"/api/trips/{trip_id}").json()["deleted"] == 1
    assert api_client.delete(f"/api/trips/{trip_id}").json()["deleted"] == 0


def test_deleting_a_rental_needs_the_token(api_client, monkeypatch, session, jerry):
    monkeypatch.delenv("TOLLS_TOKEN", raising=False)
    trip_id = api_client.post("/api/trips", json=_body()).json()["trip"]["id"]
    monkeypatch.setenv("TOLLS_TOKEN", "letmein")
    assert api_client.delete(f"/api/trips/{trip_id}").status_code == 401
    assert session.scalar(select(Trip)) is not None


# ---------------------------------------------------------------------------
# The list
# ---------------------------------------------------------------------------
def test_the_list_shows_manual_rentals_and_what_they_caught(
    api_client, monkeypatch, session, jerry
):
    """A window typed slightly wrong shows up as a rental that caught nothing,
    which is the cheapest way to notice it."""
    monkeypatch.delenv("TOLLS_TOKEN", raising=False)
    api_client.post("/api/tolls/import",
                    files={"statement": ("a.csv", _statement(), "text/csv")})
    api_client.post("/api/trips", json=_body())
    api_client.post("/api/trips", json=_body(guest_name="Nobody",
                                             starts_at="2026-09-01T13:00:00",
                                             ends_at="2026-09-01T18:00:00"))
    rows = api_client.get("/api/trips").json()["trips"]
    assert [r["guest_name"] for r in rows] == ["Priya", "Nobody"]
    assert [r["toll_count"] for r in rows] == [1, 0]


def test_turo_rentals_are_not_in_the_manual_list(api_client, monkeypatch, session, jerry):
    monkeypatch.delenv("TOLLS_TOKEN", raising=False)
    session.add(Trip(
        vehicle_id=jerry.id, guest_name="Dylan",
        starts_at=datetime(2026, 10, 4, 7, 0, tzinfo=EASTERN),
        ends_at=datetime(2026, 10, 4, 9, 0, tzinfo=EASTERN),
        state=TripState.completed, source=TripSource.email,
    ))
    session.flush()
    api_client.post("/api/trips", json=_body())
    names = [r["guest_name"] for r in api_client.get("/api/trips").json()["trips"]]
    assert names == ["Priya"]
    both = [r["guest_name"]
            for r in api_client.get("/api/trips?manual_only=false").json()["trips"]]
    assert sorted(both) == ["Dylan", "Priya"]


def test_a_rental_still_running_is_marked_active(api_client, monkeypatch, session, jerry):
    """So the run sheet shows the car as out with somebody, not sitting free."""
    monkeypatch.delenv("TOLLS_TOKEN", raising=False)
    now = datetime.now(tz=EASTERN)
    api_client.post("/api/trips", json=_body(
        starts_at=(now - timedelta(hours=1)).isoformat(),
        ends_at=(now + timedelta(hours=5)).isoformat(),
    ))
    assert session.scalars(select(Trip)).one().state is TripState.active


def test_earnings_are_optional_and_kept(api_client, monkeypatch, session, jerry):
    monkeypatch.delenv("TOLLS_TOKEN", raising=False)
    api_client.post("/api/trips", json=_body(earnings_cents=18000))
    assert session.scalars(select(Trip)).one().earnings_cents == 18000


def test_a_crossing_already_billed_to_another_guest_is_not_stolen(
    api_client, monkeypatch, session, jerry
):
    """Recording a rental must not reassign a crossing that already has an
    owner, however the windows overlap. Rematching only touches what nothing
    has claimed."""
    monkeypatch.delenv("TOLLS_TOKEN", raising=False)
    session.add(Trip(
        vehicle_id=jerry.id, guest_name="Dylan",
        starts_at=datetime(2026, 10, 4, 13, 0, tzinfo=EASTERN),
        ends_at=datetime(2026, 10, 4, 15, 0, tzinfo=EASTERN),
        state=TripState.completed, source=TripSource.email,
    ))
    session.flush()
    api_client.post("/api/tolls/import",
                    files={"statement": ("a.csv", _statement(), "text/csv")})
    assert session.scalars(select(Toll)).one().trip.guest_name == "Dylan"

    api_client.post("/api/trips", json=_body(guest_name="Priya"))
    assert session.scalars(select(Toll)).one().trip.guest_name == "Dylan"


def test_a_finished_rental_is_marked_completed(api_client, monkeypatch, session, jerry):
    """State is not only cosmetic: the run sheet reads it to decide whether a
    car is out with somebody, and a past rental left marked upcoming would show
    a car as about to be collected."""
    monkeypatch.delenv("TOLLS_TOKEN", raising=False)
    api_client.post("/api/trips", json=_body())  # October 4th, in the past
    assert session.scalars(select(Trip)).one().state is TripState.completed


def test_a_future_rental_is_marked_upcoming(api_client, monkeypatch, session, jerry):
    monkeypatch.delenv("TOLLS_TOKEN", raising=False)
    soon = datetime.now(tz=EASTERN) + timedelta(days=3)
    api_client.post("/api/trips", json=_body(
        starts_at=soon.isoformat(),
        ends_at=(soon + timedelta(days=1)).isoformat(),
    ))
    assert session.scalars(select(Trip)).one().state is TripState.upcoming
