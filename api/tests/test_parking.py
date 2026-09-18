"""Parking derivation and move-task generation.

Geometry here uses the fleet's real reported positions, because the design
claim being tested — that a GPS fix cannot resolve side of street — is a claim
about these streets and this hardware.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, time, timedelta

import pytest
from sqlalchemy import select

from turonomics_api.db.models import (
    AspRule,
    AspSuspension,
    ParkingSession,
    RuleSource,
    StreetSegmentSide,
    StreetSide,
    Task,
    TaskKind,
    TaskState,
    Trip,
    TripSource,
    TripState,
    Vehicle,
)
from turonomics_api.ingest.parking import (
    close_parking_session,
    confirm_side,
    open_parking_session,
    resolve_side,
)
from turonomics_api.ingest.tasks import refresh_move_task

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL") and not os.environ.get("DATABASE_URL"),
    reason="no database configured",
)

# Jimmy's real fix, 590 Bergen St.
JIMMY_LAT, JIMMY_LON = 40.679865, -73.970204
NORTH_LAT, SOUTH_LAT = 40.679900, 40.679800
FAR_LAT = 40.684000  # a few blocks away


def _curb(lat: float) -> str:
    return f"SRID=4326;LINESTRING(-73.9715 {lat}, -73.9690 {lat})"


@pytest.fixture
def bergen(session):
    north = StreetSegmentSide(
        street_name="Bergen St",
        from_cross_street="Carlton Ave",
        to_cross_street="Vanderbilt Ave",
        side=StreetSide.north,
        geom=_curb(NORTH_LAT),
    )
    south = StreetSegmentSide(
        street_name="Bergen St",
        from_cross_street="Carlton Ave",
        to_cross_street="Vanderbilt Ave",
        side=StreetSide.south,
        geom=_curb(SOUTH_LAT),
    )
    session.add_all([north, south])
    session.flush()
    return north, south


@pytest.fixture
def jimmy(session):
    v = Vehicle(nickname="Jimmy", make="Toyota", model="4-Runner", year=2023, plate="LEH9892")
    session.add(v)
    session.flush()
    return v


def _rule(session, side, days, start, end, source=RuleSource.captured):
    session.add(
        AspRule(
            segment_side_id=side.id,
            days_of_week=list(days),
            starts_at=start,
            ends_at=end,
            source=source,
            confidence=1.0,
        )
    )
    session.flush()


# ---------------------------------------------------------------------------
# Side resolution
# ---------------------------------------------------------------------------


def test_the_guess_is_reported_as_ambiguous_for_a_real_fix(session, bergen):
    """The measured case: two curbs 3.9 m and 7.2 m away. A nearest-side rule
    would pick one, and be wrong about half the time."""
    guess = resolve_side(session, lat=JIMMY_LAT, lon=JIMMY_LON)
    assert guess.segment_side is not None
    assert guess.is_ambiguous is True
    assert guess.confidence < 0.5


def test_an_unambiguous_fix_gets_a_high_confidence(session, bergen):
    """Parked hard against one curb with the other side far off."""
    guess = resolve_side(session, lat=NORTH_LAT, lon=-73.9702)
    assert guess.segment_side.side is StreetSide.north
    assert guess.is_ambiguous is False
    assert guess.confidence > 0.7


def test_nothing_in_range_is_not_a_guess(session, bergen):
    guess = resolve_side(session, lat=FAR_LAT, lon=-73.9702)
    assert guess.segment_side is None
    assert guess.confidence == 0.0


def test_a_van_is_not_offered_a_spot_it_does_not_fit(session, bergen):
    """NYC has no length-based cleaning rule, so van fit is a property of the
    spot, not the rule."""
    north, south = bergen
    north.fits_van = False
    session.flush()
    guess = resolve_side(session, lat=NORTH_LAT, lon=-73.9702, needs_large_spot=True)
    assert guess.segment_side.side is StreetSide.south


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------


def test_trip_end_opens_a_session_and_trip_start_closes_it(session, bergen, jimmy):
    at = datetime(2026, 9, 15, 2, 30, tzinfo=UTC)
    opened = open_parking_session(session, vehicle=jimmy, lat=JIMMY_LAT, lon=JIMMY_LON, at=at)
    assert opened.ended_at is None
    assert opened.guessed_segment_side_id is not None
    assert opened.segment_side_id is None  # a guess is not a confirmation

    closed = close_parking_session(session, vehicle=jimmy, at=at + timedelta(hours=6))
    assert closed.id == opened.id
    assert closed.ended_at is not None
    assert closed.must_move_by is None


def test_a_repeated_trip_end_does_not_open_a_second_session(session, bergen, jimmy):
    """Bouncie retries webhooks for up to 11 hours, so duplicates are routine."""
    at = datetime(2026, 9, 15, 2, 30, tzinfo=UTC)
    first = open_parking_session(session, vehicle=jimmy, lat=JIMMY_LAT, lon=JIMMY_LON, at=at)
    again = open_parking_session(session, vehicle=jimmy, lat=JIMMY_LAT, lon=JIMMY_LON, at=at)
    assert again.id == first.id


def test_gps_jitter_on_a_stationary_car_is_not_a_new_spot(session, bergen, jimmy):
    at = datetime(2026, 9, 15, 2, 30, tzinfo=UTC)
    first = open_parking_session(session, vehicle=jimmy, lat=JIMMY_LAT, lon=JIMMY_LON, at=at)
    # ~8 m north, well inside the device's own error.
    jittered = open_parking_session(
        session, vehicle=jimmy, lat=JIMMY_LAT + 0.00007, lon=JIMMY_LON, at=at + timedelta(minutes=5)
    )
    assert jittered.id == first.id


def test_moving_to_a_genuinely_different_spot_starts_a_new_session(session, bergen, jimmy):
    at = datetime(2026, 9, 15, 2, 30, tzinfo=UTC)
    first = open_parking_session(session, vehicle=jimmy, lat=JIMMY_LAT, lon=JIMMY_LON, at=at)
    moved = open_parking_session(
        session, vehicle=jimmy, lat=FAR_LAT, lon=-73.9702, at=at + timedelta(hours=2)
    )
    assert moved.id != first.id
    assert session.get(ParkingSession, first.id).ended_at is not None


def test_a_correction_is_recorded_as_one(session, bergen, jimmy):
    north, south = bergen
    at = datetime(2026, 9, 15, 2, 30, tzinfo=UTC)
    ps = open_parking_session(session, vehicle=jimmy, lat=JIMMY_LAT, lon=JIMMY_LON, at=at)
    guessed = ps.guessed_segment_side_id
    other = south if guessed == north.id else north

    confirm_side(session, parking_session=ps, segment_side=other, confirmed_at=at)
    assert ps.was_corrected is True
    assert ps.segment_side_id == other.id


def test_confirming_the_guess_is_not_a_correction(session, bergen, jimmy):
    north, south = bergen
    at = datetime(2026, 9, 15, 2, 30, tzinfo=UTC)
    ps = open_parking_session(session, vehicle=jimmy, lat=JIMMY_LAT, lon=JIMMY_LON, at=at)
    same = north if ps.guessed_segment_side_id == north.id else south
    confirm_side(session, parking_session=ps, segment_side=same, confirmed_at=at)
    assert ps.was_corrected is False


# ---------------------------------------------------------------------------
# Move tasks
# ---------------------------------------------------------------------------


def test_no_deadline_until_the_side_is_confirmed(session, bergen, jimmy):
    """An unconfirmed guess is a coin flip, and a confidently wrong deadline is
    worse than an admitted gap."""
    north, _ = bergen
    _rule(session, north, (2, 5), time(8, 0), time(9, 30))
    at = datetime(2026, 9, 15, 2, 30, tzinfo=UTC)
    open_parking_session(session, vehicle=jimmy, lat=JIMMY_LAT, lon=JIMMY_LON, at=at)

    assert refresh_move_task(session, vehicle=jimmy, now=at) is None


def test_a_confirmed_side_produces_a_deadline(session, bergen, jimmy):
    north, _ = bergen
    _rule(session, north, (2, 5), time(8, 0), time(9, 30))  # Tue & Fri 8:00-9:30
    at = datetime(2026, 9, 15, 11, 42, tzinfo=UTC)  # 07:42 local, Tuesday
    ps = open_parking_session(session, vehicle=jimmy, lat=JIMMY_LAT, lon=JIMMY_LON, at=at)
    confirm_side(session, parking_session=ps, segment_side=north, confirmed_at=at)

    task = refresh_move_task(session, vehicle=jimmy, now=at)
    assert task is not None
    assert task.kind is TaskKind.asp_move
    assert task.state is TaskState.open
    assert task.due_by.astimezone(UTC) == datetime(2026, 9, 15, 12, 0, tzinfo=UTC)  # 08:00 EDT
    assert "north side" in task.location_label
    assert session.get(ParkingSession, ps.id).must_move_by is not None


def test_the_other_side_gives_a_completely_different_deadline(session, bergen, jimmy):
    """Why the confirmation tap exists: the same fix, the other side, and an
    18-minute emergency becomes a two-day non-event."""
    north, south = bergen
    _rule(session, north, (2, 5), time(8, 0), time(9, 30))
    _rule(session, south, (1, 4), time(11, 30), time(13, 0))
    at = datetime(2026, 9, 15, 11, 42, tzinfo=UTC)

    ps = open_parking_session(session, vehicle=jimmy, lat=JIMMY_LAT, lon=JIMMY_LON, at=at)
    confirm_side(session, parking_session=ps, segment_side=south, confirmed_at=at)
    task = refresh_move_task(session, vehicle=jimmy, now=at)

    assert task.due_by.astimezone(UTC) == datetime(2026, 9, 17, 15, 30, tzinfo=UTC)  # Thu 11:30 EDT
    assert (task.due_by - at) > timedelta(days=1)


def test_regenerating_updates_the_task_rather_than_duplicating_it(session, bergen, jimmy):
    north, _ = bergen
    _rule(session, north, (2, 5), time(8, 0), time(9, 30))
    at = datetime(2026, 9, 15, 11, 42, tzinfo=UTC)
    ps = open_parking_session(session, vehicle=jimmy, lat=JIMMY_LAT, lon=JIMMY_LON, at=at)
    confirm_side(session, parking_session=ps, segment_side=north, confirmed_at=at)

    first = refresh_move_task(session, vehicle=jimmy, now=at)
    second = refresh_move_task(session, vehicle=jimmy, now=at + timedelta(minutes=5))
    assert first.id == second.id
    assert len(session.scalars(select(Task)).all()) == 1


def test_a_suspension_pushes_the_deadline_out(session, bergen, jimmy):
    north, _ = bergen
    _rule(session, north, (2, 5), time(8, 0), time(9, 30))
    session.add(AspSuspension(suspended_on=datetime(2026, 9, 15).date(), reason="Holiday"))
    at = datetime(2026, 9, 15, 11, 42, tzinfo=UTC)
    ps = open_parking_session(session, vehicle=jimmy, lat=JIMMY_LAT, lon=JIMMY_LON, at=at)
    confirm_side(session, parking_session=ps, segment_side=north, confirmed_at=at)

    task = refresh_move_task(session, vehicle=jimmy, now=at)
    assert task.due_by.astimezone(UTC) == datetime(2026, 9, 18, 12, 0, tzinfo=UTC)  # Friday


# ---------------------------------------------------------------------------
# The cross-module rule
# ---------------------------------------------------------------------------


def test_a_car_on_a_guest_trip_is_suppressed(session, bergen, jimmy):
    """The reason every module emits Task and nothing else: this is one rule,
    not four modules aware of each other."""
    north, _ = bergen
    _rule(session, north, (2, 5), time(8, 0), time(9, 30))
    at = datetime(2026, 9, 15, 11, 42, tzinfo=UTC)
    ps = open_parking_session(session, vehicle=jimmy, lat=JIMMY_LAT, lon=JIMMY_LON, at=at)
    confirm_side(session, parking_session=ps, segment_side=north, confirmed_at=at)
    session.add(
        Trip(
            vehicle_id=jimmy.id,
            guest_name="Dana W.",
            starts_at=at - timedelta(hours=1),
            ends_at=at + timedelta(days=2),
            state=TripState.active,
            source=TripSource.manual,
        )
    )
    session.flush()

    task = refresh_move_task(session, vehicle=jimmy, now=at)
    assert task.state is TaskState.suppressed
    assert "Dana W." in task.suppressed_reason


def test_the_obligation_returns_when_the_trip_ends(session, bergen, jimmy):
    """Suppression must not be a one-way door, or the car silently loses its
    parking clock the moment a guest ever drove it."""
    north, _ = bergen
    _rule(session, north, (2, 5), time(8, 0), time(9, 30))
    at = datetime(2026, 9, 15, 11, 42, tzinfo=UTC)
    ps = open_parking_session(session, vehicle=jimmy, lat=JIMMY_LAT, lon=JIMMY_LON, at=at)
    confirm_side(session, parking_session=ps, segment_side=north, confirmed_at=at)
    trip = Trip(
        vehicle_id=jimmy.id,
        guest_name="Dana W.",
        starts_at=at - timedelta(hours=1),
        ends_at=at + timedelta(hours=2),
        state=TripState.active,
        source=TripSource.manual,
    )
    session.add(trip)
    session.flush()

    assert refresh_move_task(session, vehicle=jimmy, now=at).state is TaskState.suppressed

    later = at + timedelta(hours=3)  # trip is over
    trip.state = TripState.completed
    session.flush()
    task = refresh_move_task(session, vehicle=jimmy, now=later)
    assert task.state is TaskState.open
    assert task.suppressed_reason is None


def test_a_task_can_be_built_from_a_parking_session_read_back_from_the_database(
    session, bergen, jimmy
):
    """Regression: Task.location copies the parking session's geometry, and a
    value that has round-tripped through the database comes back as WKB rather
    than as the string it went in as. Writing that back needs Shapely.

    Every other test in this file builds the session in memory and hands the
    geometry over as a string, so the round trip never happened and the failure
    only appeared against real data.
    """
    north, _ = bergen
    _rule(session, north, (1, 4), time(11, 30), time(13, 0))
    at = datetime(2026, 9, 18, 10, 0, tzinfo=UTC)
    ps = open_parking_session(session, vehicle=jimmy, lat=JIMMY_LAT, lon=JIMMY_LON, at=at)
    confirm_side(session, parking_session=ps, segment_side=north, confirmed_at=at)
    session.commit()

    # Force the geometry to come back from Postgres rather than from identity map.
    session.expire_all()
    reloaded = session.get(Vehicle, jimmy.id)

    task = refresh_move_task(session, vehicle=reloaded, now=at)
    session.commit()
    assert task is not None
    assert task.due_by is not None
    assert task.location is not None


def test_what_was_confirmed_here_before_beats_the_nearest_kerb(session, bergen, jimmy):
    """The operator puts the real-world hit rate of a distance-based guess at
    about 60/40 — the kerbs are ~10 m apart and the device's error is
    comparable. Someone who stood on the street and answered is better evidence
    than a 1 m difference in distance, so a past confirmation at this spot wins.
    """
    north, south = bergen
    at = datetime(2026, 9, 18, 2, 0, tzinfo=UTC)

    # A position the geometry reads as north, confirmed by hand as south.
    first = open_parking_session(session, vehicle=jimmy, lat=NORTH_LAT, lon=-73.9702, at=at)
    assert session.get(StreetSegmentSide, first.guessed_segment_side_id).side is StreetSide.north
    confirm_side(session, parking_session=first, segment_side=south, confirmed_at=at)
    close_parking_session(session, vehicle=jimmy, at=at + timedelta(hours=1))
    session.commit()

    # Parking in the same place again should now default to what was answered.
    guess = resolve_side(session, lat=NORTH_LAT, lon=-73.9702)
    assert guess.segment_side.side is StreetSide.south
    assert guess.remembered is True
    assert guess.times_confirmed == 1
    # Raised, but deliberately not certain: the same spot can be the other side
    # today, and the two are metres apart.
    assert 0.8 <= guess.confidence < 1.0


def test_memory_does_not_reach_across_to_a_different_block(session, bergen, jimmy):
    north, south = bergen
    at = datetime(2026, 9, 18, 2, 0, tzinfo=UTC)
    ps = open_parking_session(session, vehicle=jimmy, lat=NORTH_LAT, lon=-73.9702, at=at)
    confirm_side(session, parking_session=ps, segment_side=south, confirmed_at=at)
    close_parking_session(session, vehicle=jimmy, at=at + timedelta(hours=1))
    session.commit()

    # Far enough away to be a different spot entirely.
    guess = resolve_side(session, lat=NORTH_LAT, lon=-73.9660)
    assert guess.remembered is False


def test_a_guess_from_memory_is_recorded_as_such(session, bergen, jimmy):
    """Stored so the two kinds of guess can be scored separately later."""
    north, south = bergen
    at = datetime(2026, 9, 18, 2, 0, tzinfo=UTC)
    ps = open_parking_session(session, vehicle=jimmy, lat=NORTH_LAT, lon=-73.9702, at=at)
    assert ps.guess_from_memory is False
    confirm_side(session, parking_session=ps, segment_side=south, confirmed_at=at)
    close_parking_session(session, vehicle=jimmy, at=at + timedelta(hours=1))
    session.commit()

    again = open_parking_session(session, vehicle=jimmy, lat=NORTH_LAT, lon=-73.9702,
                                 at=at + timedelta(hours=2))
    assert again.guess_from_memory is True
