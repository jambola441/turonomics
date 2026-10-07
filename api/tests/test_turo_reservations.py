"""Tests for finding every reservation on the account from Turo's own lists.

Until this, a trip existed here only if a Turo email had mentioned it, and the
pull could not add one because it only asked Turo about reservations already
on file. Shapes are the ones the operator's probe of turo.com/us/en/trips
reported, with values masked; the values here are invented.
"""

from __future__ import annotations

from datetime import UTC, datetime
from datetime import timedelta as td
from typing import Any

import pytest

from turonomics_api.db.models import Trip, TripSource, TripState, Vehicle
from turonomics_api.ingest.turo_reservations import (
    ReservationsResult,
    apply_reservations,
    find_reservations,
    parse_reservation,
)

from .conftest import requires_db

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)


def _moment(when: datetime) -> dict[str, Any]:
    return {
        "epochMillis": int(when.timestamp() * 1000),
        "localDate": f"{when:%Y-%m-%d}",
        "localTime": f"{when:%H:%M}",
    }


def _span(start: datetime, end: datetime) -> dict[str, Any]:
    return {"start": _moment(start), "end": _moment(end)}


def _vehicle(listing: int = 3001001, plate: str = "LWH4685", vin: str = "VIN00000000000001"):
    # Nested ids everywhere: the vehicle, its image, the people. None of them
    # has a span, and none may be read as a reservation.
    return {
        "id": listing, "make": "Toyota", "model": "Corolla", "year": 2024,
        "image": {"id": 99, "verified": True},
        "registration": {"licensePlate": plate, "state": "NY"},
        "vin": vin,
    }


def _trip(rid: int, start: datetime, end: datetime, *, status="COMPLETED", **vehicle) -> dict:
    """One `tripHistoryFeeds.list[].trips[]` entry, as probed."""
    return {
        "id": rid,
        "booking": _span(start, end),
        "interval": _span(start, end),
        "request": _span(start, end),
        # Present on bookings that were never cancelled; it means nothing alone.
        "cancelledRequest": _span(start, end),
        "hasPendingChangeRequest": False,
        "statusCode": status,
        "statusSummary": "Cancelled by guest" if status == "CANCELLED" else None,
        "renter": {"id": 555, "firstName": "Shashi", "name": "Shashi"},
        "owner": {"id": 777, "firstName": "Host"},
        "vehicle": _vehicle(**vehicle),
    }


def _history(*months: list[dict], num_pages: int = 3) -> dict:
    return {
        "hostedAndCoHostedVehicles": [_vehicle()],
        "tripHistoryFeeds": {
            "list": [{"month": {"month": 6, "year": 2026}, "trips": trips} for trips in months],
            "numPages": num_pages,
        },
    }


def _upcoming_item(rid: int, start: datetime, end: datetime, kind: str) -> dict:
    """One `upcomingTripItems[]` entry: an event naming its reservation."""
    return {
        "reservationId": rid,
        "interval": _span(start, end),
        "upcomingTripFeedItemType": kind,
        "actor": {"id": 556, "firstName": "Jenna"},
        "vehicle": _vehicle(),
        "inProgress": False,
    }


JUNE = datetime(2026, 6, 1, 14, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Finding them in a body
# ---------------------------------------------------------------------------


def test_trip_history_is_read_through_its_month_groups() -> None:
    body = _history(
        [_trip(1, JUNE, JUNE + td(days=2)), _trip(2, JUNE + td(days=3), JUNE + td(days=5))],
        [_trip(3, JUNE + td(days=9), JUNE + td(days=10))],
    )
    assert [r["id"] for r in find_reservations(body)] == [1, 2, 3]


def test_nested_ids_are_not_reservations() -> None:
    """The vehicle, its image and the people all carry integer ids."""
    body = _history([_trip(1, JUNE, JUNE + td(days=2))])
    found = list(find_reservations(body))
    assert len(found) == 1
    assert "hostedAndCoHostedVehicles" in body, "a list of cars beside the trips"


def test_an_upcoming_item_is_a_reservation_by_its_reservation_id() -> None:
    body = {"upcomingTripItems": [
        _upcoming_item(7, NOW + td(days=2), NOW + td(days=4), "OWNER_TRIP_START"),
        _upcoming_item(7, NOW + td(days=2), NOW + td(days=4), "OWNER_TRIP_END"),
    ]}
    parsed = [parse_reservation(r) for r in find_reservations(body)]
    assert {p.reservation_id for p in parsed if p} == {"7"}
    assert parsed[0] is not None and parsed[0].guest_name == "Jenna"


def test_the_conversation_feed_shape_is_read_too() -> None:
    body = {"list": [{"conversationId": "c", "messageCount": 3,
                      "reservation": _trip(4, JUNE, JUNE + td(days=1), status="BOOKED")}]}
    assert [r["id"] for r in find_reservations(body)] == [4]


def test_a_reservation_reads_its_car_and_guest() -> None:
    parsed = parse_reservation(_trip(1, JUNE, JUNE + td(days=2), plate="lwh 4685"))
    assert parsed is not None
    assert parsed.reservation_id == "1"
    assert parsed.starts_at == JUNE and parsed.ends_at == JUNE + td(days=2)
    assert parsed.plate == "LWH4685" and parsed.listing_id == "3001001"
    assert parsed.guest_name == "Shashi"
    assert parsed.cancelled is False, "a cancelledRequest span alone is not a cancellation"


def test_cancelled_is_read_off_the_status() -> None:
    parsed = parse_reservation(_trip(1, JUNE, JUNE + td(days=2), status="CANCELLED"))
    assert parsed is not None and parsed.cancelled is True


def test_the_booking_outranks_the_request() -> None:
    """A rental extended after it was requested: the booking is the truth."""
    entry = _trip(1, JUNE, JUNE + td(days=2))
    entry["booking"] = _span(JUNE, JUNE + td(days=4))
    parsed = parse_reservation(entry)
    assert parsed is not None and parsed.ends_at == JUNE + td(days=4)


# ---------------------------------------------------------------------------
# Applying them
# ---------------------------------------------------------------------------


@pytest.fixture()
def car(session):
    vehicle = Vehicle(
        nickname="Jimmy", make="Toyota", model="Corolla", year=2024,
        plate="LWH4685", vin="VIN00000000000001", turo_listing_id="3001001",
    )
    session.add(vehicle)
    session.flush()
    return vehicle


def _apply(session, body):
    result = ReservationsResult()
    ids = apply_reservations(session, body, now=NOW, result=result)
    return ids, result


@requires_db
def test_a_reservation_the_mail_never_mentioned_is_added(session, car) -> None:
    ids, result = _apply(session, _history([_trip(1, JUNE, JUNE + td(days=2))]))
    assert ids == ["1"] and result.created == ["1"]
    trip = session.query(Trip).one()
    assert trip.turo_trip_id == "1" and trip.vehicle_id == car.id
    assert trip.state is TripState.completed
    assert trip.source is TripSource.extension
    assert trip.guest_name == "Shashi"


@requires_db
def test_a_known_reservation_is_left_to_the_detail_pull(session, car) -> None:
    session.add(Trip(
        vehicle_id=car.id, turo_trip_id="1", guest_name="From the email",
        starts_at=JUNE, ends_at=JUNE + td(days=3), state=TripState.completed,
        source=TripSource.email,
    ))
    session.flush()
    _, result = _apply(session, _history([_trip(1, JUNE, JUNE + td(days=2))]))
    assert result.known == 1 and result.created == []
    trip = session.query(Trip).one()
    assert trip.ends_at == JUNE + td(days=3) and trip.guest_name == "From the email"


@requires_db
@pytest.mark.parametrize(
    ("vehicle", "how"),
    [
        ({"listing": 3001001, "plate": "XXX0000", "vin": "NOPE"}, "listing id"),
        ({"listing": 1, "plate": "LWH-4685", "vin": "NOPE"}, "plate"),
        ({"listing": 1, "plate": "XXX0000", "vin": "vin00000000000001"}, "vin"),
    ],
)
def test_the_car_is_found_by_any_of_its_identifiers(session, car, vehicle, how) -> None:
    _, result = _apply(session, _history([_trip(1, JUNE, JUNE + td(days=2), **vehicle)]))
    assert result.created == ["1"], how


@requires_db
def test_a_car_outside_the_fleet_is_reported_not_guessed(session, car) -> None:
    body = _history([_trip(1, JUNE, JUNE + td(days=2), listing=1, plate="ABC1234", vin="X")])
    _, result = _apply(session, body)
    assert result.created == [] and result.unmatched == ["1: ABC1234"]
    assert session.query(Trip).count() == 0


@requires_db
def test_states_come_from_the_status_and_the_dates(session, car) -> None:
    body = _history([
        _trip(1, JUNE, JUNE + td(days=2), status="CANCELLED"),
        _trip(2, NOW - td(days=1), NOW + td(days=1), status="BOOKED"),
        _trip(3, NOW + td(days=3), NOW + td(days=5), status="BOOKED"),
    ])
    _apply(session, body)
    states = {t.turo_trip_id: t.state for t in session.query(Trip)}
    assert states == {
        "1": TripState.cancelled, "2": TripState.active, "3": TripState.upcoming,
    }


@requires_db
def test_an_upcoming_rental_named_twice_is_added_once(session, car) -> None:
    body = {"upcomingTripItems": [
        _upcoming_item(7, NOW + td(days=2), NOW + td(days=4), "OWNER_TRIP_START"),
        _upcoming_item(7, NOW + td(days=2), NOW + td(days=4), "OWNER_TRIP_END"),
    ]}
    ids, result = _apply(session, body)
    assert ids == ["7"] and result.created == ["7"]


# ---------------------------------------------------------------------------
# The endpoint
# ---------------------------------------------------------------------------


@requires_db
def test_the_endpoint_says_what_a_page_held(api_client, monkeypatch, session, car) -> None:
    monkeypatch.delenv("TOLLS_TOKEN", raising=False)
    body = _history([_trip(1, JUNE, JUNE + td(days=2)), _trip(2, JUNE, JUNE + td(days=1))],
                    num_pages=4)
    out = api_client.post("/api/turo/reservations", json={"body": body})
    assert out.status_code == 200, out.text
    payload = out.json()
    assert payload["ids"] == ["1", "2"]
    assert payload["created"] == ["1", "2"]
    assert payload["num_pages"] == 4


@requires_db
def test_the_pull_is_told_where_the_lists_are(api_client) -> None:
    lists = api_client.get("/api/turo/wanted").json()["reservation_lists"]
    assert any("trip-history" in path and "{page}" in path for path in lists)
    assert any("upcoming-trips" in path for path in lists)


@requires_db
def test_the_endpoint_needs_the_token(api_client, monkeypatch) -> None:
    monkeypatch.setenv("TOLLS_TOKEN", "s3cret")
    assert api_client.post("/api/turo/reservations", json={"body": {}}).status_code == 401
