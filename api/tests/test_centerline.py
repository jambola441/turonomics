"""Signs with no coordinates, placed on the street centreline.

The block that started this is Jimmy's: St Marks Place between Fifth and Sixth
Avenues, whose eleven broom signs have no position, so the car was offered
Prospect Place, 70 metres off. The fixtures below are that block's real
centreline piece and the avenues at either end of it.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from turonomics_api.asp.centerline import (
    Streets,
    load_unplaced,
    pieces_from_rows,
    placed_count,
    street_base,
)
from turonomics_api.asp.ingest import load_signs
from turonomics_api.db.models import AspRule, StreetSegmentSide, StreetSide
from turonomics_api.ingest.parking import resolve_side

from .conftest import requires_db

FIFTH_CORNER = (-73.977456, 40.680861)
SIXTH_CORNER = (-73.974959, 40.679885)
JIMMY = {"lat": 40.68044, "lon": -73.976358}


def line(name, *coords, pid="1", width="30"):
    return {"full_street_name": name, "physicalid": pid, "streetwidth": width,
            "the_geom": {"type": "MultiLineString", "coordinates": [[list(c) for c in coords]]}}


CENTERLINE = [
    line("ST MARKS AVE", FIFTH_CORNER, SIXTH_CORNER, pid="100770"),
    line("5 AVE", FIFTH_CORNER, (-73.9780, 40.6815), pid="2"),
    line("5 AVE", (-73.9770, 40.6802), FIFTH_CORNER, pid="3"),
    line("6 AVE", SIXTH_CORNER, (-73.9755, 40.6805), pid="4"),
    # The next block east, also St Marks, which must not be taken for this one.
    line("ST MARKS AVE", SIXTH_CORNER, (-73.9735, 40.6793), pid="5"),
    line("FLATBUSH AVE", (-73.9735, 40.6793), (-73.9740, 40.6800), pid="6"),
    # Prospect Place, a block north: where the car was being sent.
    line("PROSPECT PL", (-73.9783, 40.6822), (-73.9758, 40.6812), pid="7"),
]


def broom(side, day="WEDNESDAY", on="ST MARKS PLACE", frm="5 AVENUE", to="6 AVENUE"):
    return {"on_street": on, "from_street": frm, "to_street": to, "side_of_street": side,
            "sign_description": f"NO PARKING (SANITATION BROOM SYMBOL) {day} 11AM-12:30PM -->",
            "distance_from_intersection": "40", "record_type": "Current"}


@pytest.mark.parametrize(("name", "base"), [
    ("ST MARKS PLACE", "ST MARKS"),
    ("SAINT MARKS AVENUE", "ST MARKS"),
    ("ST MARKS AVE", "ST MARKS"),
    ("5 AVENUE", "5"),
    ("PLAZA ST EAST", "PLZ ST E"),
    ("FLATBUSH AVENUE EXTENSION", "FLATBUSH AVE EXT"),
    ("PLAZA", "PLZ"),
])
def test_names_compare_without_their_type_word(name, base) -> None:
    assert street_base(name) == base


def test_the_block_runs_between_its_two_cross_streets() -> None:
    streets = Streets(pieces_from_rows(CENTERLINE))
    block = streets.block("ST MARKS PLACE", "5 AVENUE", "6 AVENUE")
    assert block is not None and [p.physical_id for p in block] == ["100770"]
    # Either way round.
    assert [p.physical_id for p in streets.block("ST MARKS PLACE", "6 AVENUE", "5 AVENUE")] == [
        "100770"]
    # The next block east is its own.
    assert [p.physical_id for p in streets.block("ST MARKS AVENUE", "6 AVENUE",
                                                 "FLATBUSH AVENUE")] == ["5"]


def test_a_block_split_in_pieces_is_chained() -> None:
    mid = (-73.9762, 40.68037)
    rows = [line("ST MARKS AVE", FIFTH_CORNER, mid, pid="a"),
            line("ST MARKS AVE", mid, SIXTH_CORNER, pid="b"),
            *CENTERLINE[1:4]]
    block = Streets(pieces_from_rows(rows)).block("ST MARKS PLACE", "5 AVENUE", "6 AVENUE")
    assert block is not None and {p.physical_id for p in block} == {"a", "b"}


def test_names_that_do_not_chain_are_not_guessed() -> None:
    streets = Streets(pieces_from_rows(CENTERLINE))
    assert streets.block("ST MARKS PLACE", "5 AVENUE", "FLATBUSH AVENUE") is not None, (
        "two pieces is still one walk")
    assert streets.block("ST MARKS PLACE", "5 AVENUE", "7 AVENUE") is None
    assert streets.block("PROSPECT PLACE", "5 AVENUE", "6 AVENUE") is None, (
        "a street that touches neither cross street")
    assert streets.block("ST MARKS PLACE", "DEAD END", "6 AVENUE") is None


@requires_db
def test_jimmys_block_is_placed_and_he_is_on_it(session) -> None:
    report = load_unplaced(session, [broom("N"), broom("S", "TUESDAY")], CENTERLINE)
    assert report.placed.block_sides_created == 2 and report.placed.rules_created == 2
    assert placed_count(session) == 2

    guess = resolve_side(session, lat=JIMMY["lat"], lon=JIMMY["lon"])
    assert guess.segment_side is not None
    assert guess.segment_side.street_name == "ST MARKS PLACE"
    assert guess.distance_m < 10
    # Each kerb is on its own side of the street's centre.
    lat = {s.side: session.scalar(select(func.ST_Y(func.ST_Centroid(func.geometry(
        StreetSegmentSide.geom)))).where(StreetSegmentSide.id == s.id))
        for s in session.scalars(select(StreetSegmentSide))}
    centre = (FIFTH_CORNER[1] + SIXTH_CORNER[1]) / 2
    assert lat[StreetSide.north] > centre > lat[StreetSide.south]
    north = session.scalar(select(StreetSegmentSide).where(
        StreetSegmentSide.side == StreetSide.north))
    assert [r.days_of_week for r in north.rules] == [[3]]


@requires_db
def test_the_kerbs_are_half_the_roadbed_from_the_centre(session) -> None:
    """A 30-foot roadbed puts each kerb 15 feet, about 4.6 metres, out."""
    load_unplaced(session, [broom("N"), broom("S", "TUESDAY")], CENTERLINE[:4])
    for segment in session.scalars(select(StreetSegmentSide)):
        gap = session.scalar(select(func.ST_Distance(
            segment.geom,
            func.ST_GeogFromText(f"LINESTRING({FIFTH_CORNER[0]} {FIFTH_CORNER[1]}, "
                                 f"{SIXTH_CORNER[0]} {SIXTH_CORNER[1]})"))))
        assert gap == pytest.approx(4.57, abs=0.3)


@requires_db
def test_a_block_traced_from_its_own_signs_is_left_alone(session) -> None:
    """Signs with coordinates trace the kerb itself, which beats an offset
    centreline, and the sign loader already took every sign on the block."""
    load_signs(session, [{**broom("N"), "sign_x_coord": 990800, "sign_y_coord": 187200},
                         {**broom("N"), "sign_x_coord": 990900, "sign_y_coord": 187150,
                          "distance_from_intersection": "140"}])
    report = load_unplaced(session, [broom("N"), broom("S", "TUESDAY")], CENTERLINE)
    assert report.already_loaded == 1 and report.placed.block_sides_created == 1
    north = session.scalar(select(StreetSegmentSide).where(
        StreetSegmentSide.side == StreetSide.north))
    assert north.nyc_physical_id is None


@requires_db
def test_placing_again_refreshes_rather_than_adds(session) -> None:
    """How a forced reload picks up a changed sign on a placed block."""
    load_unplaced(session, [broom("N"), broom("S", "TUESDAY")], CENTERLINE)
    again = load_unplaced(session, [broom("N", "THURSDAY"), broom("S", "TUESDAY")], CENTERLINE)
    assert again.already_loaded == 0 and again.placed.block_sides_created == 0
    assert session.scalar(select(func.count()).select_from(StreetSegmentSide)) == 2
    assert session.scalar(select(func.count()).select_from(AspRule)) == 2
    north = session.scalar(select(StreetSegmentSide).where(
        StreetSegmentSide.side == StreetSide.north))
    session.refresh(north)
    assert [r.days_of_week for r in north.rules] == [[4]]
