"""Tests for deciding when a car came back from the tracker.

This narrows who gets billed for a crossing after a rental's end, so both
directions matter: a car home early must stop absorbing the operator's own
driving, and a car still out must keep absorbing the guest's.

The asymmetry is deliberate and tested: the tracker can narrow the window the
fixed grace allows, never widen it. No tracker can say who is holding the keys,
and billing a guest for somebody else's driving is the expensive mistake.
"""

from __future__ import annotations

from datetime import UTC, datetime
from datetime import timedelta as td
from zoneinfo import ZoneInfo

import pytest

from turonomics_api.db.models import (
    ParkingSession,
    Toll,
    Trip,
    TripSource,
    TripState,
    Vehicle,
)
from turonomics_api.ingest.returns import SETTLED_FOR, settled_at
from turonomics_api.ingest.tolls import _trip_overrunning

from .conftest import requires_db

NOW = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
ENDS = NOW - td(hours=12)
GRACE = td(hours=2)


@pytest.fixture()
def car(session):
    vehicle = Vehicle(
        nickname="Jolene", make="Toyota", model="Corolla", year=2024, plate="LWH4685"
    )
    session.add(vehicle)
    session.flush()
    return vehicle


@pytest.fixture()
def rental(session, car):
    trip = Trip(
        vehicle_id=car.id,
        turo_trip_id="58358939",
        guest_name="Dylan",
        starts_at=ENDS - td(days=2),
        ends_at=ENDS,
        state=TripState.completed,
        source=TripSource.email,
    )
    session.add(trip)
    session.flush()
    return trip


# A parking session has to have a place — the deadline machinery is built on
# "which side of which block", so a session without one is not a thing the
# schema allows. 11238, near the operator's own street.
HOME = "SRID=4326;POINT(-73.9655 40.6782)"


def _parked(session, car, *, at, until=None):
    row = ParkingSession(
        vehicle_id=car.id, started_at=at, ended_at=until, location=HOME
    )
    session.add(row)
    session.flush()
    return row


# ---------------------------------------------------------------------------
# When the car came to rest
# ---------------------------------------------------------------------------


@requires_db
def test_with_no_tracker_data_the_fixed_grace_stands(session, car) -> None:
    """The behaviour this replaces, and still the one for a car whose dongle is
    unplugged or whose statement arrives before the telemetry."""
    found = settled_at(session, car.id, ends_at=ENDS, grace=GRACE, now=NOW)
    assert found.at == ENDS + GRACE
    assert found.from_tracker is False


@requires_db
def test_a_car_that_came_to_rest_and_stayed_sets_the_moment(session, car) -> None:
    _parked(session, car, at=ENDS + td(minutes=20), until=ENDS + td(hours=9))
    found = settled_at(session, car.id, ends_at=ENDS, grace=GRACE, now=NOW)
    assert found.at == ENDS + td(minutes=20)
    assert found.from_tracker is True


@requires_db
def test_a_stop_for_coffee_is_not_a_return(session, car) -> None:
    """Bouncie polls often enough that a short errand is its own session.
    Treating one as the return would end a rental in the middle of it."""
    _parked(session, car, at=ENDS + td(minutes=10), until=ENDS + td(minutes=18))
    _parked(session, car, at=ENDS + td(minutes=50), until=ENDS + td(hours=8))
    found = settled_at(session, car.id, ends_at=ENDS, grace=GRACE, now=NOW)
    assert found.at == ENDS + td(minutes=50), "the one that lasted"


@requires_db
def test_a_car_nobody_has_moved_since_is_plainly_back(session, car) -> None:
    _parked(session, car, at=ENDS + td(minutes=30), until=None)
    found = settled_at(session, car.id, ends_at=ENDS, grace=GRACE, now=NOW)
    assert found.at == ENDS + td(minutes=30)
    assert found.from_tracker is True


@requires_db
def test_a_session_opened_moments_ago_is_not_yet_a_return(session, car) -> None:
    """An open session only counts once it has lasted as long as a closed one
    would have to. Otherwise a car that has just pulled over reads as returned."""
    recent = NOW - td(minutes=5)
    _parked(session, car, at=recent, until=None)
    found = settled_at(session, car.id, ends_at=recent - td(minutes=1), grace=GRACE, now=NOW)
    assert found.from_tracker is False


@requires_db
def test_coming_to_rest_after_the_grace_is_not_consulted(session, car) -> None:
    """The ceiling. A car driven all evening has no quiet moment inside the
    window, and the tracker may not extend a rental past what the guess already
    allowed — it cannot say who is holding the keys."""
    _parked(session, car, at=ENDS + td(hours=5), until=None)
    found = settled_at(session, car.id, ends_at=ENDS, grace=GRACE, now=NOW)
    assert found.at == ENDS + GRACE
    assert found.from_tracker is False


@requires_db
def test_parking_before_the_rental_ended_is_not_the_return(session, car) -> None:
    _parked(session, car, at=ENDS - td(hours=3), until=ENDS - td(hours=1))
    found = settled_at(session, car.id, ends_at=ENDS, grace=GRACE, now=NOW)
    assert found.from_tracker is False


@requires_db
def test_the_threshold_is_the_published_one(session, car) -> None:
    """Pinned against the constant rather than a literal, so changing the
    constant changes the behaviour and not just this test."""
    _parked(session, car, at=ENDS + td(minutes=5), until=ENDS + td(minutes=5) + SETTLED_FOR)
    assert settled_at(session, car.id, ends_at=ENDS, grace=GRACE, now=NOW).from_tracker
    session.query(ParkingSession).delete()
    _parked(
        session,
        car,
        at=ENDS + td(minutes=5),
        until=ENDS + td(minutes=5) + SETTLED_FOR - td(seconds=1),
    )
    assert not settled_at(session, car.id, ends_at=ENDS, grace=GRACE, now=NOW).from_tracker


@requires_db
def test_another_cars_parking_is_not_this_cars_return(session, car) -> None:
    other = Vehicle(nickname="Jerry", make="Toyota", model="Corolla", year=2025, plate="LZA7293")
    session.add(other)
    session.flush()
    _parked(session, other, at=ENDS + td(minutes=10), until=None)
    found = settled_at(session, car.id, ends_at=ENDS, grace=GRACE, now=NOW)
    assert found.from_tracker is False


# ---------------------------------------------------------------------------
# What it does to a crossing
# ---------------------------------------------------------------------------


@requires_db
def test_a_crossing_after_the_car_was_home_is_not_the_guests(session, car, rental) -> None:
    """The errand case. The car was back twenty minutes after the booking
    ended; an hour later it was the operator driving, and the fixed two-hour
    window used to hand that to the guest."""
    _parked(session, car, at=ENDS + td(minutes=20), until=ENDS + td(hours=6))
    found = _trip_overrunning(
        session, car.id, ENDS + td(minutes=75), GRACE, now=NOW
    )
    assert found is None


@requires_db
def test_a_crossing_before_the_car_was_home_is_the_guests(session, car, rental) -> None:
    _parked(session, car, at=ENDS + td(minutes=90), until=ENDS + td(hours=6))
    found = _trip_overrunning(
        session, car.id, ENDS + td(minutes=75), GRACE, now=NOW
    )
    assert found is not None and found.id == rental.id


@requires_db
def test_without_tracker_data_the_crossing_is_still_the_guests(
    session, car, rental
) -> None:
    """No regression for a fleet, or a car, the tracker says nothing about."""
    found = _trip_overrunning(
        session, car.id, ENDS + td(minutes=75), GRACE, now=NOW
    )
    assert found is not None and found.id == rental.id


@requires_db
def test_a_crossing_exactly_when_the_car_stopped_is_the_guests(
    session, car, rental
) -> None:
    """They were still in it. The boundary belongs to the person who drove
    there, and the engine going off is the end of that drive, not before it."""
    stopped = ENDS + td(minutes=40)
    _parked(session, car, at=stopped, until=ENDS + td(hours=6))
    found = _trip_overrunning(session, car.id, stopped, GRACE, now=NOW)
    assert found is not None and found.id == rental.id


@requires_db
def test_the_import_path_uses_it(api_client, monkeypatch, session, car, rental) -> None:
    """Through the endpoint, because `_claim` is where the grace is read and a
    test on the private function alone would not catch it being skipped."""
    monkeypatch.delenv("TOLLS_TOKEN", raising=False)
    monkeypatch.setenv("TOLL_OVERRUN_GRACE_MINUTES", "120")
    _parked(session, car, at=ENDS + td(minutes=20), until=ENDS + td(hours=6))
    session.commit()

    late = ENDS + td(minutes=75)
    # The real statement's columns, as the importer's own tests use them.
    header = (
        "Lane Txn ID,Tag/Plate #,Agency,Entry Plaza,Exit Plaza,Class,Date,"
        "Exit Time,Amount\n"
    )
    eastern = late.astimezone(ZoneInfo("America/New_York"))
    row = (
        f"900,NY {car.plate},MTAB&T,,RKB,31,{eastern:%m/%d/%Y},"
        f"{eastern:%I:%M:%S %p},$-4.50\n"
    )
    response = api_client.post(
        "/api/tolls/import",
        files={"statement": ("a.csv", (header + row).encode(), "text/csv")},
    )
    assert response.status_code == 200, response.text
    stored = session.scalars(select_toll()).all()
    assert len(stored) == 1
    assert stored[0].trip_id is None, "the car was already home"


def select_toll():
    from sqlalchemy import select

    return select(Toll)


@requires_db
def test_the_first_durable_rest_wins_not_the_last(session, car) -> None:
    """Two real rests inside the window, and the earlier one is taken.

    This is a deliberate under-bill rather than an oversight. A guest who
    parked half an hour for dinner and then drove home genuinely returned at
    the second rest, and taking the first costs the operator the crossings in
    between. The alternative is billing a guest for driving that may have been
    somebody else's, which is a dispute rather than a shortfall — the tracker
    cannot say who held the keys, so it is only ever allowed to narrow.

    Without this the ordering is untested: reversing it leaves every other test
    in this file passing, because they have at most one qualifying session.
    """
    _parked(session, car, at=ENDS + td(minutes=20), until=ENDS + td(minutes=55))
    _parked(session, car, at=ENDS + td(minutes=80), until=ENDS + td(hours=9))
    found = settled_at(session, car.id, ends_at=ENDS, grace=GRACE, now=NOW)
    assert found.at == ENDS + td(minutes=20)
    assert found.from_tracker is True


@requires_db
def test_a_statement_imported_weeks_later_still_reads_the_return(
    session, car
) -> None:
    """The normal case, not an edge one: E-ZPass statements arrive weeks after
    the crossings. An open parking session from the night of the rental is the
    strongest evidence there is, however long ago it opened."""
    _parked(session, car, at=ENDS + td(minutes=25), until=None)
    much_later = NOW + td(days=30)
    found = settled_at(session, car.id, ends_at=ENDS, grace=GRACE, now=much_later)
    assert found.at == ENDS + td(minutes=25)
    assert found.from_tracker is True
