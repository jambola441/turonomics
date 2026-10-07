"""Tests for a rental's route and where its tolls go on it.

The placement is an estimate — even speed within one drive — so what is
tested hard is that it is on the right drive, at the right end of it, and that
nothing is placed where nothing honest can say.
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
    along,
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


def test_along_measures_by_distance_not_by_point_count() -> None:
    start = along(LINE, 0.0)
    end = along(LINE, 1.0)
    middle = along(LINE, 0.5)
    assert start == LINE[0] and end == LINE[-1]
    # Half the distance travelled, to the metre — whichever leg that falls on.
    legs = [_metres(LINE[0], LINE[1]), _metres(LINE[1], LINE[2])]
    if middle[0] >= LINE[1][0]:
        travelled = _metres(LINE[0], middle)
    else:
        travelled = legs[0] + _metres(LINE[1], middle)
    assert abs(travelled - sum(legs) / 2) < 1.0
    assert along(LINE, 7.0) == LINE[-1], "clamped"


def _route(*drives: Drive) -> Route:
    return Route(drives=list(drives), source="bouncie")


DRIVE = Drive(starts_at=T0, ends_at=T0 + td(minutes=40), points=LINE)


def test_a_crossing_at_an_unknown_plaza_goes_on_the_drive_under_way() -> None:
    late = place(T0 + td(minutes=38), "XYZ", _route(DRIVE))
    assert late is not None and late.how == "route"
    assert abs(late.lat - LINE[-1][0]) < 0.01, "near the end of the drive, as timed"


def test_a_known_plaza_beats_the_estimate_when_the_two_agree() -> None:
    """The Verrazzano gantry is a kilometre or two from where the even-speed
    estimate puts the car; the gantry is where the charge was."""
    placed = place(T0 + td(minutes=38), "VNB", _route(DRIVE))
    assert placed is not None and placed.how == "plaza"
    assert (placed.lat, placed.lon) == (40.6022, -74.0628)
    assert placed.name and "Verrazzano" in placed.name
    assert placed.source and placed.source.startswith("https://")


def test_a_plaza_far_from_where_the_car_was_is_flagged_and_the_track_used() -> None:
    """A code that means something else on another road: the tracker had the
    car in Brooklyn, and the plaza says the Thruway at Albany."""
    placed = place(T0 + td(minutes=38), "24", _route(DRIVE))
    assert placed is not None and placed.how == "route"
    assert placed.off_route_km is not None and placed.off_route_km > 100


def test_a_zone_charge_goes_on_the_track_or_else_the_zone() -> None:
    on_track = place(T0 + td(minutes=20), "CRZ", _route(DRIVE))
    assert on_track is not None and on_track.how == "route"
    off_track = place(T0, "CRZ", Route())
    assert off_track is not None and off_track.how == "zone"


def test_a_crossing_slightly_outside_a_drive_is_still_that_drive() -> None:
    """The statement's clock against the tracker's."""
    placed = place(T0 + td(minutes=45), "XYZ", _route(DRIVE))
    assert placed is not None and placed.how == "route"
    assert placed.lat == LINE[-1][0], "clamped to the end"


def test_without_a_route_a_known_plaza_is_placed_at_the_plaza() -> None:
    placed = place(T0, "rkb", Route())
    assert placed is not None and placed.how == "plaza"
    assert (round(placed.lat, 2), round(placed.lon, 2)) == (40.80, -73.92), "the gantry"


def test_an_unknown_plaza_with_no_route_is_not_guessed() -> None:
    assert place(T0, "583", Route()) is None
    assert place(T0 + td(hours=5), "583", _route(DRIVE)) is None, "no drive at that time"


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
    _toll(session, car, rental, T0 + td(minutes=39), "XYZ")
    session.commit()
    out = api_client.get(f"/api/trips/{rental.id}/map").json()
    assert out["route_source"] == "telemetry"
    assert len(out["drives"]) == 1 and len(out["drives"][0]["points"]) == 3
    [toll] = out["tolls"]
    assert toll["how"] == "route"
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


def test_a_crossing_just_before_a_drive_is_placed_at_its_start_not_beyond_it() -> None:
    placed = place(T0 - td(minutes=4), "XYZ", _route(DRIVE))
    assert placed is not None
    assert placed.lat == LINE[0][0] and placed.lon == LINE[0][1]


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
