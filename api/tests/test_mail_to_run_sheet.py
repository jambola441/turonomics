"""Mail in, suppressed street-cleaning alert out.

This is the chain the whole email module exists for, and the one cross-module
rule the Task abstraction was built to make possible: a car a guest is driving
must not nag the operator to move it. Everything upstream — the probe, the
parser, the vehicle match — is in service of this working.
"""

from __future__ import annotations

import base64
import os
from datetime import UTC, datetime, time, timedelta

import pytest
from sqlalchemy import func, select

from turonomics_api.db.models import (
    AspRule,
    ParkingSession,
    RuleSource,
    StreetSegmentSide,
    StreetSide,
    Task,
    TaskState,
    Trip,
    TripState,
    Vehicle,
)
from turonomics_api.ingest.mail import sync_trips_from_mail
from turonomics_api.ingest.parking import confirm_side, open_parking_session
from turonomics_api.ingest.tasks import refresh_move_task

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL") and not os.environ.get("DATABASE_URL"),
    reason="no database configured",
)

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
LAT, LON = 40.679884, -73.970193

BOOKING = """\
Ka-ching! Dana's trip with your Ford Transit is booked from Oct 1, 2026, 2:00 PM \
to Oct 8, 2026, 4:00 PM.
You earn: $284.00
Ford Transit 2024
booked by Dana
Reservation ID #12345
"""

CANCEL = """\
Trip start: Oct 1 2:00 PM
Trip end: Oct 8 4:00 PM
Ford Transit 2024
requested by Dana
Reservation ID #12345
"""


class FakeGmail:
    def __init__(self, messages: list[tuple[str, str]]):
        self.messages = messages

    def search(self, query: str, *, limit: int = 0) -> list[str]:
        return [str(i) for i in range(len(self.messages))]

    def message(self, message_id: str) -> dict:
        subject, body = self.messages[int(message_id)]
        encoded = base64.urlsafe_b64encode(body.encode()).decode().rstrip("=")
        return {
            "internalDate": str(int(NOW.timestamp() * 1000)),
            "payload": {
                "mimeType": "text/plain",
                "headers": [{"name": "Subject", "value": subject}],
                "body": {"data": encoded},
            },
        }


def _parked_van_with_a_deadline(session) -> Vehicle:
    seg = StreetSegmentSide(
        street_name="St Marks Ave",
        side=StreetSide.south,
        geom="SRID=4326;LINESTRING(-73.9715 40.679884, -73.9690 40.679884)",
    )
    session.add(seg)
    session.flush()
    session.add(
        AspRule(
            segment_side_id=seg.id,
            days_of_week=[0, 3],
            starts_at=time(8, 30),
            ends_at=time(10, 0),
            source=RuleSource.nyc_signs,
            confidence=1.0,
        )
    )
    van = Vehicle(nickname="Bubba", make="Ford", model="Transit", year=2024)
    session.add(van)
    session.flush()

    parking = open_parking_session(session, vehicle=van, lat=LAT, lon=LON, at=NOW)
    confirm_side(session, parking_session=parking, segment_side=seg, confirmed_at=NOW)
    refresh_move_task(session, vehicle=van, now=NOW)
    session.flush()
    return van


def test_a_booked_trip_suppresses_the_move_alert_for_that_car(session):
    """The point of all of it. The van is parked on a street that gets swept,
    so it has a deadline — but a guest has it from 2pm, and telling the operator
    to go and move a car a guest is driving is both impossible and the fastest
    way to teach them to ignore the run sheet."""
    van = _parked_van_with_a_deadline(session)
    open_task = session.scalar(
        select(Task).where(Task.vehicle_id == van.id, Task.state == TaskState.open)
    )
    assert open_task is not None, "a parked van on a sweeping street should have a deadline"

    during = datetime(2026, 10, 2, 9, 0, tzinfo=UTC)
    sync_trips_from_mail(
        session,
        client=FakeGmail([("Dana's trip with your Ford Transit is booked!", BOOKING)]),
        now=during,
    )

    trip = session.scalar(select(Trip))
    assert trip is not None and trip.state is TripState.active
    task = session.get(Task, open_task.id)
    assert task.state is TaskState.suppressed, "the alert must stand down while a guest has it"
    assert "Dana" in (task.suppressed_reason or ""), task.suppressed_reason


def test_cancelling_the_trip_brings_the_alert_back(session):
    """The suppression has to reverse. A guest cancelling leaves the van on the
    street with the sweeper still coming, and that is precisely when forgetting
    costs a ticket."""
    van = _parked_van_with_a_deadline(session)
    during = datetime(2026, 10, 2, 9, 0, tzinfo=UTC)
    sync_trips_from_mail(
        session,
        client=FakeGmail([("Dana's trip with your Ford Transit is booked!", BOOKING)]),
        now=during,
    )
    assert session.scalar(
        select(func.count()).select_from(Task).where(Task.state == TaskState.suppressed)
    ) == 1

    sync_trips_from_mail(
        session,
        client=FakeGmail([("Dana has canceled their trip with your Transit", CANCEL)]),
        now=during,
    )
    task = session.scalar(select(Task).where(Task.vehicle_id == van.id))
    assert task.state is TaskState.open, "a cancelled trip must restore the deadline"


def test_mail_that_carries_no_trip_changes_nothing(session):
    _parked_van_with_a_deadline(session)
    before = session.scalar(select(func.count()).select_from(Trip))
    result = sync_trips_from_mail(
        session,
        client=FakeGmail([
            ("Your earnings are on the way!", "Ka-ching! Turo sent your earnings of $284.00."),
            ("Turo: Start earning!", "Hi there, list your car."),
        ]),
        now=NOW,
    )
    assert result.created == 0 and result.updated == 0
    assert session.scalar(select(func.count()).select_from(Trip)) == before


def test_a_trip_for_a_car_not_in_the_fleet_is_counted_not_dropped(session):
    _parked_van_with_a_deadline(session)
    body = BOOKING.replace("Ford Transit 2024", "Tesla Cybertruck 2026")
    result = sync_trips_from_mail(
        session,
        client=FakeGmail([("Dana's trip with your Tesla Cybertruck is booked!", body)]),
        now=NOW,
    )
    assert result.unmatched == 1
    assert session.scalar(select(func.count()).select_from(Trip)) == 0


def test_a_disconnected_mailbox_degrades_rather_than_failing_the_poll(session):
    """Bouncie positions still work without Gmail, so a revoked grant must not
    take the whole poll down with it."""
    from turonomics_api.gmail.client import GmailNotConnected

    class Disconnected:
        def search(self, query: str, *, limit: int = 0) -> list[str]:
            raise GmailNotConnected("Gmail is not connected")

        def message(self, message_id: str) -> dict:  # pragma: no cover
            raise AssertionError("should not be reached")

    result = sync_trips_from_mail(session, client=Disconnected(), now=NOW)
    assert result.created == 0 and result.unmatched == 0


def test_the_same_mail_scanned_twice_does_not_duplicate_the_trip(session):
    """The poll runs every ten minutes over the last seven days of mail, so
    every message is read many times."""
    _parked_van_with_a_deadline(session)
    client = FakeGmail([("Dana's trip with your Ford Transit is booked!", BOOKING)])
    for _ in range(3):
        sync_trips_from_mail(session, client=client, now=NOW + timedelta(minutes=10))
    assert session.scalar(select(func.count()).select_from(Trip)) == 1


def test_a_trip_that_has_ended_stops_suppressing(session):
    van = _parked_van_with_a_deadline(session)
    after = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
    sync_trips_from_mail(
        session,
        client=FakeGmail([("Dana's trip with your Ford Transit is booked!", BOOKING)]),
        now=after,
    )
    assert session.scalar(select(Trip)).state is TripState.completed
    task = session.scalar(select(Task).where(Task.vehicle_id == van.id))
    assert task.state is TaskState.open


def test_parking_still_resolves_while_a_guest_has_the_car(session):
    """Suppression is about the alert, not the data. The parking session stays
    open so the spot is remembered for when the car comes back."""
    van = _parked_van_with_a_deadline(session)
    during = datetime(2026, 10, 2, 9, 0, tzinfo=UTC)
    sync_trips_from_mail(
        session,
        client=FakeGmail([("Dana's trip with your Ford Transit is booked!", BOOKING)]),
        now=during,
    )
    parking = session.scalar(
        select(ParkingSession).where(
            ParkingSession.vehicle_id == van.id, ParkingSession.ended_at.is_(None)
        )
    )
    assert parking is not None and parking.segment_side_id is not None


def test_the_sync_reports_itself_even_when_nothing_lands(session, caplog):
    """The first live run logged nothing at all, because the summary only fired
    when a count was non-zero. "No trip mail this week" and "the parser
    rejected every message" produced identical silence, and telling them apart
    cost a deploy. The summary is unconditional now, and a run that parses
    nothing says why the first message was skipped.
    """
    import logging

    _parked_van_with_a_deadline(session)
    with caplog.at_level(logging.INFO, logger="turonomics.ingest.mail"):
        sync_trips_from_mail(
            session,
            client=FakeGmail([
                ("Your earnings are on the way!", "Ka-ching! Turo sent your earnings."),
                ("Turo: Start earning!", "Hi there, list your car."),
            ]),
            now=NOW,
        )
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "2 message(s)" in logged, logged
    assert "2 not a trip" in logged, logged
    assert "nothing parsed" in logged, logged


def test_the_skip_reason_names_the_kind_not_the_subject_text(session, caplog):
    """A diagnostic that leaks the guest's name back into the log would undo
    the probe's whole point."""
    import logging

    _parked_van_with_a_deadline(session)
    with caplog.at_level(logging.INFO, logger="turonomics.ingest.mail"):
        sync_trips_from_mail(
            session,
            client=FakeGmail([
                ("Dana has sent you a message about your Transit", "no reservation here"),
            ]),
            now=NOW,
        )
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "message" in logged, logged
    assert "Dana" not in logged, logged
