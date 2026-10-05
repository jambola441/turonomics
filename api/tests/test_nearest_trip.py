"""Guessing whose crossing an unattributed toll was.

A toll twenty minutes after a rental ended is usually the guest still driving:
Turo's end time is when they marked the car returned, not when they stopped
using it. A toll three days out is the operator's own errand, or an
off-platform rental nobody recorded. Both are unattributed, and the only thing
telling them apart is the gap.

A hint, never an attribution. These tests are mostly about it staying one.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from turonomics_api.db.models import TripState
from turonomics_api.ingest.tolls import NEAREST_TRIP_WINDOW, nearest_trip

from .conftest import requires_db

NOON = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)


class FakeTrip:
    """Enough of a Trip to answer the question, with no database involved.

    `nearest_trip` takes the rentals rather than querying for them precisely so
    that it can be exercised like this.
    """

    def __init__(self, guest, starts, ends, state=TripState.completed):
        self.id = uuid.uuid4()
        self.guest_name = guest
        self.starts_at = starts
        self.ends_at = ends
        self.state = state


def test_a_crossing_just_after_a_rental_names_that_guest() -> None:
    trip = FakeTrip("Dylan", NOON - timedelta(hours=4), NOON - timedelta(minutes=20))
    near = nearest_trip([trip], NOON)
    assert near is not None
    assert near.guest_name == "Dylan"
    assert near.relation == "after"
    assert near.gap_seconds == 20 * 60


def test_a_crossing_just_before_a_rental_names_that_guest() -> None:
    """Which happens: a guest collects the car early, or the start time is the
    booking's rather than the handover's."""
    trip = FakeTrip("Jane", NOON + timedelta(minutes=10), NOON + timedelta(hours=5))
    near = nearest_trip([trip], NOON)
    assert near is not None
    assert near.guest_name == "Jane"
    assert near.relation == "before"
    assert near.gap_seconds == 10 * 60


def test_the_closer_rental_wins() -> None:
    far = FakeTrip("Far", NOON - timedelta(days=1), NOON - timedelta(hours=20))
    near_trip_ = FakeTrip("Near", NOON - timedelta(hours=3), NOON - timedelta(minutes=5))
    assert nearest_trip([far, near_trip_], NOON).guest_name == "Near"
    # Order of the list must not decide it.
    assert nearest_trip([near_trip_, far], NOON).guest_name == "Near"


def test_a_rental_that_contains_the_crossing_is_skipped() -> None:
    """That crossing is already attributed. Reporting a gap of zero against the
    trip it is billed to would be a hint about nothing."""
    containing = FakeTrip("Dylan", NOON - timedelta(hours=1), NOON + timedelta(hours=1))
    assert nearest_trip([containing], NOON) is None


def test_a_containing_rental_does_not_hide_a_neighbouring_one() -> None:
    """Skipping the container must not abandon the search."""
    containing = FakeTrip("Dylan", NOON - timedelta(hours=1), NOON + timedelta(hours=1))
    other = FakeTrip("Jane", NOON + timedelta(hours=2), NOON + timedelta(hours=6))
    near = nearest_trip([containing, other], NOON)
    assert near is not None and near.guest_name == "Jane"


def test_nothing_is_reported_beyond_the_window() -> None:
    """Past a few days the nearest rental says nothing useful, and a label
    would be noise dressed as a clue."""
    trip = FakeTrip("Old", NOON - NEAREST_TRIP_WINDOW - timedelta(days=2),
                    NOON - NEAREST_TRIP_WINDOW - timedelta(days=1))
    assert nearest_trip([trip], NOON) is None


def test_the_window_is_inclusive_at_its_edge() -> None:
    trip = FakeTrip("Edge", NOON - NEAREST_TRIP_WINDOW - timedelta(hours=1),
                    NOON - NEAREST_TRIP_WINDOW)
    assert nearest_trip([trip], NOON) is not None


def test_a_cancelled_rental_is_not_a_hint() -> None:
    """Nobody drove it, so nobody owes the toll."""
    trip = FakeTrip("Ghost", NOON - timedelta(hours=2), NOON - timedelta(minutes=1),
                    state=TripState.cancelled)
    assert nearest_trip([trip], NOON) is None


def test_no_rentals_at_all_is_not_an_error() -> None:
    assert nearest_trip([], NOON) is None


def test_a_rental_with_no_guest_name_still_gives_the_gap() -> None:
    """An off-platform rental may be recorded without a name. The gap is the
    useful half anyway."""
    trip = FakeTrip(None, NOON - timedelta(hours=2), NOON - timedelta(minutes=30))
    near = nearest_trip([trip], NOON)
    assert near is not None
    assert near.guest_name is None
    assert near.gap_seconds == 30 * 60


# ---------------------------------------------------------------------------
# The overrun lookup, called on its own
# ---------------------------------------------------------------------------
# `_claim` tries exact containment first, which makes one of the guards inside
# `_trip_overrunning` unreachable by that route. It is still the contract of the
# function, and a direct caller that lost it would bill the morning's guest for
# a crossing during the afternoon's rental — so it is tested here rather than
# left to the caller's ordering.


@requires_db
def test_a_handover_in_between_ends_the_overrun(session):
    from datetime import timedelta as td

    from turonomics_api.db.models import Trip, TripSource, Vehicle
    from turonomics_api.ingest.tolls import _trip_overrunning

    car = Vehicle(nickname="Jerry", make="Toyota", model="Corolla", year=2025,
                  plate="LZA7293")
    session.add(car)
    session.flush()

    def trip(guest, starts, ends):
        t = Trip(vehicle_id=car.id, guest_name=guest, starts_at=starts, ends_at=ends,
                 state=TripState.completed, source=TripSource.manual)
        session.add(t)
        return t

    # Morning ends at 08:00. Afternoon runs 09:00 to 11:00. The crossing is at
    # 10:30 — inside Afternoon's rental, and inside Morning's six-hour grace.
    trip("Morning", NOON - td(hours=6), NOON - td(hours=4))
    trip("Afternoon", NOON - td(hours=3), NOON - td(hours=1))
    session.flush()

    found = _trip_overrunning(
        session, car.id, NOON - td(minutes=90), td(hours=6), now=NOON
    )
    assert found is None, "Morning cannot be billed while Afternoon has the car"


@requires_db
def test_without_a_handover_the_overrun_stands(session):
    """The other half, so the test above is not passing on a quirk."""
    from datetime import timedelta as td

    from turonomics_api.db.models import Trip, TripSource, Vehicle
    from turonomics_api.ingest.tolls import _trip_overrunning

    car = Vehicle(nickname="Jolene", make="Toyota", model="Corolla", year=2025,
                  plate="LWH4685")
    session.add(car)
    session.flush()
    session.add(Trip(vehicle_id=car.id, guest_name="Morning",
                     starts_at=NOON - td(hours=6), ends_at=NOON - td(hours=4),
                     state=TripState.completed, source=TripSource.manual))
    session.flush()

    found = _trip_overrunning(
        session, car.id, NOON - td(minutes=90), td(hours=6), now=NOON
    )
    assert found is not None and found.guest_name == "Morning"
