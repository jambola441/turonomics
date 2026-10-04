"""Parsing Turo's notification emails into trips.

The bodies here follow docs/design/02-turo-email-shapes.md, which came from the
real mailbox. The values are invented; the structure is not, and that is the
part a parser can get wrong without anyone noticing.
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from turonomics_api.db.models import TripState
from turonomics_api.gmail.parse import (
    BOOKED,
    CANCELLED,
    CHANGE_REQUESTED,
    CHANGED,
    LICENCE,
    MESSAGE,
    PAYOUT,
    ParseError,
    classify,
    parse_email,
)

ET = ZoneInfo("America/New_York")
RECEIVED = datetime(2026, 10, 1, 14, 2, tzinfo=UTC)

BOOKING = """\
Dana's trip is booked.

Ka-ching! Dana's trip with your Ford Transit is booked from Oct 5, 2026, 10:00 AM \
to Oct 8, 2026, 4:00 PM.

You earn $284.00.

Trip start: Oct 5 10:00 AM
Trip end: Oct 8 4:00 PM
You earn: $284.00
Mileage included: 600 miles
View Dana's profile: https://turo.com/d/1
Send Dana a message: https://turo.com/t/1

Ford Transit 2024
booked by Dana
Dana Whitfield
(917) 555-0188
Reservation ID #12345
"""

# A message notification: same block, but no prose range — so the year has to
# be inferred. This is the common case by volume.
MESSAGE_ONLY = """\
Dana has sent you a message about your Transit.

all good, see you then

Reply https://turo.com/t/1

Trip start: Oct 5 10:00 AM
Trip end: Oct 8 4:00 PM
You earn: $284.00
Mileage included: 600 miles

Ford Transit 2024
booked by Dana
Reservation ID #12345
"""

CANCELLATION = """\
Dana has canceled their trip.

Trip start: Oct 5 10:00 AM
Trip end: Oct 8 4:00 PM
View Dana's profile: https://turo.com/d/1

Ford Transit 2024
requested by Dana
Reservation ID #12345
"""


def test_the_subject_decides_the_kind():
    cases = [
        ("Dana's trip with your Ford Transit is booked!", BOOKED),
        ("Dana has sent you a message about your Transit", MESSAGE),
        ("Dana has canceled their trip with your Transit", CANCELLED),
        ("Dana has requested a change to their trip with your Transit", CHANGE_REQUESTED),
        ("Dana has changed their trip with your Transit (2)", CHANGED),
        ("Dana confirmed your change request with your Transit", CHANGED),
        ("Dana has added another driver to their trip with your Transit", CHANGED),
        ("You still need to confirm your guest's license", LICENCE),
        ("Your earnings are on the way!", PAYOUT),
    ]
    for subject, expected in cases:
        assert classify(subject) == expected, subject


def test_requesting_a_change_is_not_the_same_as_having_changed_one():
    """Both subjects contain "change". Ordering the patterns wrongly would file
    a request as a completed change and move the trip's dates on a change the
    operator has not accepted yet."""
    assert classify("Dana has requested a change to their trip with your Transit") == (
        CHANGE_REQUESTED
    )
    assert classify("Dana has changed their trip with your Transit (2)") == CHANGED


def test_a_booking_parses_fully():
    trip = parse_email(subject="Dana's trip with your Ford Transit is booked!",
                       body=BOOKING, received_at=RECEIVED)
    assert trip.kind == BOOKED
    assert trip.reservation_id == "12345"
    assert trip.guest_name == "Dana"
    assert trip.starts_at == datetime(2026, 10, 5, 10, 0, tzinfo=ET)
    assert trip.ends_at == datetime(2026, 10, 8, 16, 0, tzinfo=ET)
    assert trip.state is TripState.upcoming
    assert trip.vehicle_text == "Ford Transit 2024"
    assert trip.earnings_cents == 28400
    assert trip.year_was_explicit is True
    assert trip.assumed_timezone == "America/New_York"


def test_the_prose_range_is_preferred_over_the_labels():
    """The labels carry no year. Reading them when the prose is available would
    throw away the one piece of information that makes the dates unambiguous."""
    trip = parse_email(subject="Dana's trip is booked!", body=BOOKING, received_at=RECEIVED)
    assert trip.year_was_explicit is True


def test_labels_alone_still_parse_with_the_year_inferred():
    trip = parse_email(subject="Dana has sent you a message about your Transit",
                       body=MESSAGE_ONLY, received_at=RECEIVED)
    assert trip.year_was_explicit is False
    assert trip.starts_at == datetime(2026, 10, 5, 10, 0, tzinfo=ET)
    assert trip.reservation_id == "12345"


def test_a_january_trip_in_a_december_email_lands_next_year():
    """The year-rollover bug, which only exists on the label path. Resolving
    "Jan 3" against a December arrival by trying the same year first puts the
    trip eleven months in the past, and a past trip is silently ignored by
    everything downstream."""
    december = datetime(2026, 12, 28, 12, 0, tzinfo=UTC)
    body = MESSAGE_ONLY.replace("Oct 5 10:00 AM", "Jan 3 10:00 AM").replace(
        "Oct 8 4:00 PM", "Jan 6 4:00 PM"
    )
    trip = parse_email(subject="Dana has sent you a message about your Transit",
                       body=body, received_at=december)
    assert trip.starts_at.year == 2027, f"resolved to {trip.starts_at}"
    assert trip.ends_at > trip.starts_at


def test_a_trip_crossing_new_year_does_not_end_before_it_starts():
    """Dec 30 to Jan 2 with inferred years: the end resolves into the start's
    year unless the crossing is handled, which the database would reject."""
    december = datetime(2026, 12, 20, 12, 0, tzinfo=UTC)
    body = MESSAGE_ONLY.replace("Oct 5 10:00 AM", "Dec 30 10:00 AM").replace(
        "Oct 8 4:00 PM", "Jan 2 4:00 PM"
    )
    trip = parse_email(subject="Dana has sent you a message about your Transit",
                       body=body, received_at=december)
    assert trip.starts_at.year == 2026
    assert trip.ends_at.year == 2027
    assert trip.ends_at > trip.starts_at


def test_a_cancellation_carries_the_same_reservation_so_it_can_be_matched():
    """The whole reason the reservation id matters: a cancellation has to find
    the booking it cancels, and it says "requested by" where a booking says
    "booked by"."""
    trip = parse_email(subject="Dana has canceled their trip with your Transit",
                       body=CANCELLATION, received_at=RECEIVED)
    assert trip.reservation_id == "12345"
    assert trip.state is TripState.cancelled
    assert trip.guest_name == "Dana"
    assert trip.earnings_cents is None, "a cancellation drops the earnings label"


def test_an_email_without_a_reservation_id_is_refused():
    """Inventing a key would create a second trip on the next email about the
    same one."""
    with pytest.raises(ParseError, match="reservation id"):
        parse_email(subject="Dana's trip is booked!",
                    body=BOOKING.replace("Reservation ID #12345", ""), received_at=RECEIVED)


def test_a_payout_is_refused_rather_than_half_parsed():
    with pytest.raises(ParseError, match="carry no trip"):
        parse_email(subject="Your earnings are on the way!",
                    body="Ka-ching! Turo sent your earnings payment of $284.00.",
                    received_at=RECEIVED)


def test_the_vehicle_falls_back_to_the_subject_when_the_body_has_no_year_line():
    body = CANCELLATION.replace("Ford Transit 2024\n", "")
    trip = parse_email(subject="Dana has canceled their trip with your Transit",
                       body=body, received_at=RECEIVED)
    assert trip.vehicle_text == "Transit"


def test_times_are_read_in_the_fleet_zone_not_utc():
    """No Turo email carries a timezone. Reading them as UTC would shift every
    deadline by four or five hours, which is the difference between a car being
    moved before the sweeper and not."""
    trip = parse_email(subject="Dana's trip is booked!", body=BOOKING,
                       received_at=RECEIVED, fleet_timezone="America/Los_Angeles")
    assert trip.starts_at.utcoffset().total_seconds() == -7 * 3600
    assert trip.assumed_timezone == "America/Los_Angeles"
