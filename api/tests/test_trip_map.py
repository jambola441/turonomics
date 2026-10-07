"""Tests for a rental's route and where its tolls go on it.

A toll goes at its plaza or nowhere. The route never moves a marker; it only
flags a plaza the car never came near, so what is tested hard is that the
check measures to the track's legs (not its sampled fixes) and that nothing is
placed where the app does not know the plaza.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from datetime import timedelta as td
from typing import Any

import pytest

from turonomics_api.db.models import TelemetryEvent, Toll, Trip, TripSource, TripState, Vehicle
from turonomics_api.ingest.trip_map import (
    BOUNCIE_WINDOW,
    Drive,
    Route,
    _metres,
    bouncie_route,
    decode_polyline,
    parse_drive,
    place,
    shape,
)

from .conftest import requires_db

T0 = datetime(2026, 7, 10, 14, 0, tzinfo=UTC)
# Brooklyn to the Verrazzano, as a straight-ish line.
LINE = [(40.6782, -73.9655), (40.6400, -74.0100), (40.6066, -74.0447)]


def test_a_polyline_decodes_as_googles_own_example() -> None:
    points = decode_polyline("_p~iF~ps|U_ulLnnqC_mqNvxq`@")
    assert points == [(38.5, -120.2), (40.7, -120.95), (43.252, -126.453)]


def test_a_drive_reads_geojson_in_lon_lat_order() -> None:
    drive = parse_drive({
        "startTime": "2026-07-10T14:00:00.000Z", "endTime": "2026-07-10T14:30:00.000Z",
        "gps": {"type": "LineString", "coordinates": [[-73.9655, 40.6782], [-74.0447, 40.6066]]},
    })
    assert drive is not None
    assert drive.points == [(40.6782, -73.9655), (40.6066, -74.0447)]
    assert drive.starts_at == T0


@pytest.mark.parametrize(
    "raw",
    [
        {},
        {"startTime": "2026-07-10T14:00:00Z", "gps": {"coordinates": [[1, 2]]}},
        {"startTime": "2026-07-10T15:00:00Z", "endTime": "2026-07-10T14:00:00Z",
         "gps": {"coordinates": [[1, 2]]}},
        {"startTime": "2026-07-10T14:00:00Z", "endTime": "2026-07-10T15:00:00Z", "gps": None},
    ],
)
def test_a_drive_without_times_or_a_track_is_not_one(raw: dict) -> None:
    assert parse_drive(raw) is None


def _route(*drives: Drive) -> Route:
    return Route(drives=list(drives), source="bouncie")


DRIVE = Drive(starts_at=T0, ends_at=T0 + td(minutes=40), points=LINE)


def test_a_known_plaza_goes_at_the_plaza_whatever_the_route_says() -> None:
    """No estimate of where the car was: the gantry is where the charge was."""
    placed = place("VNB", _route(DRIVE))
    assert placed is not None and placed.how == "plaza"
    assert (placed.lat, placed.lon) == (40.6022, -74.0628)
    assert placed.name and "Verrazzano" in placed.name
    assert placed.source and placed.source.startswith("https://")
    assert placed.off_route_km is None, "the route ends a kilometre or two from it"


def test_an_unknown_plaza_is_not_placed_even_with_a_route() -> None:
    """The time-split estimate this replaced put these miles from the truth."""
    assert place("XYZ", _route(DRIVE)) is None
    assert place("583", Route()) is None


def test_a_plaza_the_car_never_came_near_is_flagged_but_not_moved() -> None:
    """A code meaning something else on another road: the car drove Brooklyn
    to the Verrazzano, and "24" says the Thruway at Albany."""
    placed = place("24", _route(DRIVE))
    assert placed is not None and placed.how == "plaza"
    assert placed.off_route_km is not None and placed.off_route_km > 100
    assert placed.name and "Albany" in placed.name, "still drawn where the plaza is"


def test_a_gantry_between_two_fixes_is_not_flagged() -> None:
    """Tracks are sampled: the car passes the gantry between two points, and
    neither point is near it. Distance to the leg, not to the fixes."""
    plaza = (40.6022, -74.0628)
    far_a = (plaza[0] + 0.04, plaza[1] + 0.04)
    far_b = (plaza[0] - 0.04, plaza[1] - 0.04)
    sparse = Drive(starts_at=T0, ends_at=T0 + td(minutes=30), points=[far_a, far_b])
    assert _metres(far_a, plaza) > 4000 and _metres(far_b, plaza) > 4000
    placed = place("VNB", _route(sparse))
    assert placed is not None and placed.off_route_km is None


def test_without_a_route_nothing_is_flagged() -> None:
    placed = place("rkb", Route())
    assert placed is not None and placed.how == "plaza" and placed.off_route_km is None
    assert (round(placed.lat, 2), round(placed.lon, 2)) == (40.80, -73.92), "the gantry"


def test_a_zone_charge_is_the_zone_and_is_never_flagged() -> None:
    """CRZ's point is the zone's middle; a car can drive all over the zone
    without passing it."""
    placed = place("CRZ", _route(DRIVE))
    assert placed is not None and placed.how == "zone" and placed.off_route_km is None


class FakeBouncie:
    def __init__(self, drives: list[dict[str, Any]]) -> None:
        self.drives = drives
        self.asked: list[tuple[str, str]] = []

    def trips(self, imei: str, *, starts_after: str | None = None,
              ends_before: str | None = None) -> list[dict[str, Any]]:
        assert starts_after and ends_before
        self.asked.append((starts_after, ends_before))
        lo = datetime.fromisoformat(starts_after.replace("Z", "+00:00"))
        hi = datetime.fromisoformat(ends_before.replace("Z", "+00:00"))
        return [d for d in self.drives
                if lo <= datetime.fromisoformat(d["startTime"].replace("Z", "+00:00")) <= hi]


def _raw(start: datetime) -> dict[str, Any]:
    return {"startTime": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "endTime": (start + td(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "gps": {"coordinates": [[lon, lat] for lat, lon in LINE]}}


def test_a_long_rental_is_asked_for_a_week_at_a_time() -> None:
    """Bouncie refuses a range wider than a week."""
    fake = FakeBouncie([_raw(T0 + td(days=1)), _raw(T0 + td(days=9))])
    route = bouncie_route(fake, "imei", starts=T0, ends=T0 + td(days=12))
    assert len(fake.asked) == 2
    for lo, hi in fake.asked:
        span = (datetime.fromisoformat(hi.replace("Z", "+00:00"))
                - datetime.fromisoformat(lo.replace("Z", "+00:00")))
        assert span < BOUNCIE_WINDOW
    assert [d.starts_at for d in route.drives] == [T0 + td(days=1), T0 + td(days=9)]


def test_drives_bouncie_sends_in_an_unknown_shape_are_counted() -> None:
    fake = FakeBouncie([_raw(T0), {"startTime": "2026-07-10T15:00:00Z"}])
    route = bouncie_route(fake, "imei", starts=T0 - td(hours=1), ends=T0 + td(hours=3))
    assert len(route.drives) == 1
    assert route.note and "1 drive(s)" in route.note


# ---------------------------------------------------------------------------
# The endpoint
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _open(monkeypatch):
    monkeypatch.delenv("TOLLS_TOKEN", raising=False)


@pytest.fixture()
def car(session):
    vehicle = Vehicle(nickname="Jimmy", make="Toyota", model="Corolla", year=2024, plate="LWH4685")
    session.add(vehicle)
    session.flush()
    return vehicle


@pytest.fixture()
def rental(session, car):
    trip = Trip(vehicle_id=car.id, turo_trip_id="1", starts_at=T0 - td(hours=1),
                ends_at=T0 + td(hours=4), state=TripState.completed, source=TripSource.email)
    session.add(trip)
    session.flush()
    return trip


def _toll(session, car, rental, at: datetime, plaza: str) -> None:
    session.add(Toll(vehicle_id=car.id, trip_id=rental.id, occurred_at=at, plaza=plaza,
                     amount_cents=1100, fingerprint=f"t-{uuid.uuid4()}"))


@requires_db
def test_without_a_tracker_known_plazas_are_mapped_and_others_listed(
    api_client, session, car, rental
) -> None:
    _toll(session, car, rental, T0, "VNB")
    _toll(session, car, rental, T0 + td(minutes=5), "583")
    session.commit()
    out = api_client.get(f"/api/trips/{rental.id}/map").json()
    assert out["route_source"] is None and out["drives"] == []
    assert out["note"] == "no tracker on this car"
    vnb, other = out["tolls"]
    assert vnb["how"] == "plaza" and vnb["lat"] is not None
    assert other["how"] is None and other["lat"] is None


@requires_db
def test_the_trackers_stored_positions_make_a_route(api_client, session, car, rental) -> None:
    for i, (lat, lon) in enumerate(LINE):
        session.add(TelemetryEvent(
            vehicle_id=car.id, event_type="poll", occurred_at=T0 + td(minutes=20 * i),
            location=f"SRID=4326;POINT({lon} {lat})", payload={},
        ))
    _toll(session, car, rental, T0 + td(minutes=39), "VNB")
    _toll(session, car, rental, T0 + td(minutes=50), "24")
    session.commit()
    out = api_client.get(f"/api/trips/{rental.id}/map").json()
    assert out["route_source"] == "telemetry"
    assert len(out["drives"]) == 1 and len(out["drives"][0]["points"]) == 3
    vnb, albany = out["tolls"]
    assert vnb["how"] == "plaza" and vnb["off_route_km"] is None
    assert albany["how"] == "plaza" and albany["off_route_km"] > 100
    assert "fix every few minutes" in out["note"]


@requires_db
def test_another_cars_positions_are_not_this_route(api_client, session, car, rental) -> None:
    other = Vehicle(nickname="Jerry", make="Toyota", model="Corolla", year=2025, plate="LZA7293")
    session.add(other)
    session.flush()
    for i, (lat, lon) in enumerate(LINE):
        session.add(TelemetryEvent(
            vehicle_id=other.id, event_type="poll", occurred_at=T0 + td(minutes=20 * i),
            location=f"SRID=4326;POINT({lon} {lat})", payload={},
        ))
    session.commit()
    assert api_client.get(f"/api/trips/{rental.id}/map").json()["drives"] == []


@requires_db
def test_the_map_needs_the_token(api_client, monkeypatch, rental) -> None:
    """Where a car went is as private as anything Turo says about the guest."""
    monkeypatch.setenv("TOLLS_TOKEN", "s3cret")
    assert api_client.get(f"/api/trips/{rental.id}/map").status_code == 401


def test_drives_are_in_the_order_they_happened() -> None:
    fake = FakeBouncie([_raw(T0 + td(hours=2)), _raw(T0)])
    route = bouncie_route(fake, "imei", starts=T0 - td(hours=1), ends=T0 + td(hours=4))
    assert [d.starts_at for d in route.drives] == [T0, T0 + td(hours=2)]


def test_the_logged_layout_carries_no_values() -> None:
    """What goes in the log to learn Bouncie's shape must not be a route."""
    raw = {"transactionId": "abc123", "startTime": "2026-07-10T14:00:00Z",
           "distance": 12.5, "gps": {"type": "LineString",
                                     "coordinates": [[-73.9655, 40.6782]] * 40}}
    said = shape(raw)
    assert "transactionId: str(6)" in said and "distance: float" in said
    assert "coordinates: [40 × [2 × float]]" in said
    assert "40.6782" not in said and "abc123" not in said
