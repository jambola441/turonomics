"""Parsed emails becoming trips.

The two properties worth protecting: every email about one reservation produces
one trip, and a trip is never attached to a car it might not belong to. The
second matters because a trip suppresses that vehicle's street-cleaning alert —
so a wrong match means a real car quietly stops being warned about.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from turonomics_api.db.models import Trip, TripSource, TripState, Vehicle
from turonomics_api.gmail.parse import parse_email
from turonomics_api.ingest.trips import apply_parsed_trip, match_vehicle

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL") and not os.environ.get("DATABASE_URL"),
    reason="no database configured",
)

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)

BOOKING = """\
Ka-ching! Dana's trip with your Ford Transit is booked from Oct 5, 2026, 10:00 AM \
to Oct 8, 2026, 4:00 PM.
You earn: $284.00
Ford Transit 2024
booked by Dana
Reservation ID #12345
"""

CANCEL = """\
Trip start: Oct 5 10:00 AM
Trip end: Oct 8 4:00 PM
Ford Transit 2024
requested by Dana
Reservation ID #12345
"""


def _fleet(session) -> dict[str, Vehicle]:
    cars = {
        "Bubba": Vehicle(nickname="Bubba", make="Ford", model="Transit", year=2024),
        "Jimmy": Vehicle(nickname="Jimmy", make="Toyota", model="4-Runner", year=2023),
        "Jolene": Vehicle(nickname="Jolene", make="Toyota", model="Corolla", year=2025),
        "Jerry": Vehicle(nickname="Jerry", make="Toyota", model="Corolla", year=2024),
    }
    session.add_all(cars.values())
    session.flush()
    return cars


def _booking(body: str = BOOKING, subject: str = "Dana's trip with your Ford Transit is booked!"):
    return parse_email(subject=subject, body=body, received_at=NOW)


def test_a_booking_becomes_a_trip_on_the_right_car(session):
    cars = _fleet(session)
    trip = apply_parsed_trip(session, _booking(), now=NOW)
    assert trip is not None
    assert trip.vehicle_id == cars["Bubba"].id
    assert trip.turo_trip_id == "12345"
    assert trip.source is TripSource.email
    assert trip.earnings_cents == 28400
    assert trip.state is TripState.upcoming


def test_every_email_about_one_reservation_produces_one_trip(session):
    """Turo sends a booking, reminders, messages and an ending notice for the
    same trip. Each must update it, not add another."""
    _fleet(session)
    for _ in range(4):
        apply_parsed_trip(session, _booking(), now=NOW)
    assert session.scalar(select(func.count()).select_from(Trip)) == 1


def test_a_cancellation_finds_the_booking_by_reservation_id(session):
    _fleet(session)
    apply_parsed_trip(session, _booking(), now=NOW)
    cancelled = parse_email(
        subject="Dana has canceled their trip with your Transit", body=CANCEL, received_at=NOW
    )
    apply_parsed_trip(session, cancelled, now=NOW)

    trips = session.scalars(select(Trip)).all()
    assert len(trips) == 1
    assert trips[0].state is TripState.cancelled


def test_a_later_message_does_not_revive_a_cancelled_trip(session):
    """Turo keeps sending message notifications for a cancelled reservation, and
    they still carry the original dates. Letting one overwrite the state would
    put a guest back on a car that is free — and suppress its move alert."""
    _fleet(session)
    apply_parsed_trip(session, _booking(), now=NOW)
    apply_parsed_trip(
        session,
        parse_email(subject="Dana has canceled their trip with your Transit",
                    body=CANCEL, received_at=NOW),
        now=NOW,
    )
    apply_parsed_trip(
        session,
        parse_email(subject="Dana has sent you a message about your Transit",
                    body=CANCEL, received_at=NOW),
        now=NOW,
    )
    assert session.scalar(select(Trip)).state is TripState.cancelled


def test_the_dates_outrank_the_emails_label(session):
    """An "upcoming trip" reminder can arrive after the trip has started."""
    _fleet(session)
    during = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
    trip = apply_parsed_trip(session, _booking(), now=during)
    assert trip.state is TripState.active

    after = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
    trip = apply_parsed_trip(session, _booking(), now=after)
    assert trip.state is TripState.completed


# ---------------------------------------------------------------------------
# Vehicle matching, which is a guess and must behave like one
# ---------------------------------------------------------------------------


def test_the_body_line_matches_make_model_and_year(session):
    cars = _fleet(session)
    assert match_vehicle(session, "Ford Transit 2024") is cars["Bubba"]


def test_the_subject_form_without_a_year_still_matches(session):
    cars = _fleet(session)
    assert match_vehicle(session, "Transit") is cars["Bubba"]


def test_two_identical_cars_are_left_unmatched_rather_than_guessed(session):
    """This fleet has two Corollas. Attaching a trip to the wrong one would
    suppress street cleaning on a car that is actually parked on the street —
    the exact failure the alert exists to prevent. Better no match than a coin
    flip."""
    _fleet(session)
    assert match_vehicle(session, "Toyota Corolla") is None


def test_the_year_breaks_the_tie_between_two_identical_cars(session):
    cars = _fleet(session)
    assert match_vehicle(session, "Toyota Corolla 2025") is cars["Jolene"]
    assert match_vehicle(session, "Toyota Corolla 2024") is cars["Jerry"]


def test_a_car_that_is_not_in_the_fleet_matches_nothing(session):
    _fleet(session)
    assert match_vehicle(session, "Tesla Cybertruck 2026") is None


def test_an_unmatched_vehicle_reports_rather_than_inventing_a_trip(session):
    """Trip.vehicle_id is required, so there is nothing to attach the trip to.
    Returning None lets the caller count it; inventing a vehicle would put a
    phantom car in the fleet view."""
    _fleet(session)
    body = BOOKING.replace("Ford Transit 2024\n", "Tesla Cybertruck 2026\n")
    parsed = parse_email(subject="Dana's trip with your Tesla Cybertruck is booked!",
                         body=body, received_at=NOW)
    assert apply_parsed_trip(session, parsed, now=NOW) is None
    assert session.scalar(select(func.count()).select_from(Trip)) == 0
    assert session.scalar(select(func.count()).select_from(Vehicle)) == 4


def test_a_trip_on_a_matched_car_suppresses_nothing_it_should_not(session):
    """Sanity check on the thing that depends on this: the trip window is what
    the street-cleaning suppression reads, so it has to land on the right car
    with the right dates."""
    cars = _fleet(session)
    trip = apply_parsed_trip(session, _booking(), now=NOW)
    assert trip.vehicle_id == cars["Bubba"].id
    assert trip.starts_at < trip.ends_at
    assert trip.ends_at - trip.starts_at == timedelta(days=3, hours=6)
