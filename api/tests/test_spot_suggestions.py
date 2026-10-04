"""Where to move a car to, not just that you must.

The operator's problem at 8am is not "is there a deadline" — the app already
shouts about that. It is "where do I put it". These tests are mostly about the
two ways that advice could be worse than silence: ranking a block nobody has
read a sign for as the best one, and implying a space is free.
"""

from __future__ import annotations

from datetime import UTC, datetime, time

import pytest

from turonomics_api.db.models import (
    AspRule,
    RuleSource,
    StreetSegmentSide,
    StreetSide,
    Vehicle,
)
from turonomics_api.ingest.spots import suggest_spots

from .conftest import requires_db

pytestmark = requires_db

# Sunday noon, so "tomorrow" is Monday and the weekday maths is unambiguous.
NOW = datetime(2026, 10, 4, 16, 0, tzinfo=UTC)
LAT, LON = 40.679884, -73.970193


def _side(session, name, side, *, offset_deg=0.0, fits_van=None, days=None):
    """A block offset east of the car, so distance is controllable."""
    lon0 = LON + offset_deg
    row = StreetSegmentSide(
        street_name=name,
        side=side,
        fits_van=fits_van,
        geom=f"SRID=4326;LINESTRING({lon0} {LAT}, {lon0 + 0.0020} {LAT})",
    )
    session.add(row)
    session.flush()
    if days is not None:
        session.add(
            AspRule(
                segment_side_id=row.id,
                days_of_week=days,
                starts_at=time(8, 30),
                ends_at=time(10, 0),
                source=RuleSource.nyc_signs,
                confidence=1.0,
            )
        )
        session.flush()
    return row


@pytest.fixture()
def car(session):
    v = Vehicle(nickname="Jolene", make="Toyota", model="Corolla", year=2025)
    session.add(v)
    session.flush()
    return v


@pytest.fixture()
def van(session):
    v = Vehicle(
        nickname="Bubba", make="Ford", model="Transit", year=2018, needs_large_spot=True
    )
    session.add(v)
    session.flush()
    return v


def _names(spots):
    return [s.street_name for s in spots]


def test_the_block_you_can_forget_about_longest_comes_first(session, car):
    """Ranked by respite, not distance. The point is to stop thinking about
    this car for a while, and a block swept tomorrow does not achieve that."""
    _side(session, "Swept Monday", StreetSide.north, offset_deg=0.0001, days=[1])  # Monday, ISO
    _side(session, "Swept Friday", StreetSide.north, offset_deg=0.0020, days=[5])  # Friday, ISO
    spots = suggest_spots(session, vehicle=car, lat=LAT, lon=LON, now=NOW)
    assert _names(spots)[0] == "Swept Friday", "further away, but buys four more days"


def test_a_block_nobody_has_read_a_sign_for_is_not_advice(session, car):
    """It would sort first as "never swept", which is a confident answer built
    out of missing data. Park-here-we-know-nothing is worth less than silence.
    """
    _side(session, "No rules on file", StreetSide.north, offset_deg=0.0001, days=None)
    _side(session, "Swept Friday", StreetSide.north, offset_deg=0.0020, days=[5])  # Friday, ISO
    assert _names(suggest_spots(session, vehicle=car, lat=LAT, lon=LON, now=NOW)) == [
        "Swept Friday"
    ]


def test_somewhere_swept_within_the_hour_is_not_somewhere_to_move_to(session, car):
    """Respite shorter than the walk back is not respite.

    Monday 06:30 local, and this block is swept at 08:30 the same morning —
    two hours of peace, which is not a place to put a car you are trying to
    stop thinking about.
    """
    monday_dawn = datetime(2026, 10, 5, 10, 30, tzinfo=UTC)  # 06:30 in New York
    _side(session, "Swept in two hours", StreetSide.north, offset_deg=0.0001, days=[1])
    _side(session, "Swept Friday", StreetSide.south, offset_deg=0.0020, days=[5])

    spots = suggest_spots(session, vehicle=car, lat=LAT, lon=LON, now=monday_dawn)
    assert _names(spots) == ["Swept Friday"]


def test_a_van_is_not_sent_to_a_block_it_does_not_fit(session, van):
    _side(session, "Too small", StreetSide.north, offset_deg=0.0001, fits_van=False, days=[5])  # Friday, ISO
    _side(session, "Fits", StreetSide.south, offset_deg=0.0020, fits_van=True, days=[5])  # Friday, ISO
    assert _names(suggest_spots(session, vehicle=van, lat=LAT, lon=LON, now=NOW)) == ["Fits"]


def test_a_car_with_no_size_constraint_is_offered_both(session, car):
    _side(session, "Too small for a van", StreetSide.north, offset_deg=0.0001,
          fits_van=False, days=[5])  # Friday, ISO
    _side(session, "Fits anything", StreetSide.south, offset_deg=0.0020, fits_van=True, days=[5])  # Friday, ISO
    assert len(suggest_spots(session, vehicle=car, lat=LAT, lon=LON, now=NOW)) == 2


def test_the_block_it_is_already_on_is_not_a_suggestion(session, car):
    """That is the one it has to leave."""
    here = _side(session, "Current block", StreetSide.north, offset_deg=0.0001, days=[1])  # Monday, ISO
    _side(session, "Elsewhere", StreetSide.south, offset_deg=0.0020, days=[5])  # Friday, ISO
    spots = suggest_spots(
        session, vehicle=car, lat=LAT, lon=LON, now=NOW, exclude_side_id=here.id
    )
    assert _names(spots) == ["Elsewhere"]


def test_nothing_beyond_walking_distance(session, car):
    """Further and you would drive, at which point the whole neighbourhood is
    in range and the list stops being a decision."""
    _side(session, "Near", StreetSide.north, offset_deg=0.0005, days=[5])  # Friday, ISO
    _side(session, "Far", StreetSide.north, offset_deg=0.0600, days=[5])  # Friday, ISO
    assert _names(suggest_spots(session, vehicle=car, lat=LAT, lon=LON, now=NOW)) == ["Near"]


def test_the_list_is_short_enough_to_read_while_double_parked(session, car):
    for i in range(9):
        _side(session, f"Block {i}", StreetSide.north, offset_deg=0.0001 * (i + 1), days=[5])  # Friday, ISO
    assert len(suggest_spots(session, vehicle=car, lat=LAT, lon=LON, now=NOW)) == 5


def test_it_reports_distance_so_the_operator_can_overrule_the_ranking(session, car):
    """A quiet block four hundred metres away is not obviously better than a
    decent one across the street, and this does not pretend to know which."""
    _side(session, "Across the street", StreetSide.north, offset_deg=0.0002, days=[5])  # Friday, ISO
    spot = suggest_spots(session, vehicle=car, lat=LAT, lon=LON, now=NOW)[0]
    assert 0 < spot.distance_m < 400


def test_a_spot_carries_no_claim_that_a_space_is_free(session, car):
    """The app knows the rules, not the kerb. There is deliberately no field
    for availability, because inventing one would be the most useful lie it
    could tell."""
    _side(session, "Somewhere", StreetSide.north, offset_deg=0.0002, days=[5])  # Friday, ISO
    spot = suggest_spots(session, vehicle=car, lat=LAT, lon=LON, now=NOW)[0]
    assert not any(
        "free" in f or "available" in f or "empty" in f for f in vars(spot)
    ), "a field implying availability would be a promise the data cannot keep"
