"""Loading NYC sign rows into segment sides and rules.

Fixtures are shaped like the real dataset, including state plane coordinates
and the distance_from_intersection ordering.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import func, select

from turonomics_api.asp.ingest import FORWARD_EXTENSION_FT, _extend, load_signs
from turonomics_api.db.models import AspRule, RuleSource, StreetSegmentSide, StreetSide

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL") and not os.environ.get("DATABASE_URL"),
    reason="no database configured",
)

CLEAN = "NO PARKING (SANITATION BROOM SYMBOL) MONDAY THURSDAY 11:30AM-1PM <->"
CLEAN_OTHER = "NO PARKING (SANITATION BROOM SYMBOL) TUESDAY FRIDAY 11:30AM-1PM <->"


def sign(
    x,
    y,
    dist,
    desc=CLEAN,
    side="N",
    on="BERGEN STREET",
    frm="VANDERBILT AVENUE",
    to="CARLTON AVENUE",
):
    return {
        "on_street": on,
        "from_street": frm,
        "to_street": to,
        "side_of_street": side,
        "sign_description": desc,
        "distance_from_intersection": dist,
        "sign_x_coord": x,
        "sign_y_coord": y,
        "record_type": "Current",
    }


def test_a_block_side_becomes_one_segment_with_its_rule(session):
    report = load_signs(session, [sign(993000, 187000, 50), sign(993200, 187010, 200)])
    assert report.block_sides == 1
    assert report.rules_created == 1

    seg = session.scalar(select(StreetSegmentSide))
    assert seg.street_name == "BERGEN STREET"
    assert seg.side is StreetSide.north
    rule = session.scalar(select(AspRule))
    assert rule.days_of_week == [1, 4]
    assert rule.source is RuleSource.nyc_signs


def test_the_two_sides_of_one_street_are_separate_segments(session):
    """The whole point: the sides carry different rules and must not merge."""
    load_signs(
        session,
        [
            sign(993000, 187000, 50, CLEAN, side="N"),
            sign(993200, 187010, 200, CLEAN, side="N"),
            sign(993000, 186990, 50, CLEAN_OTHER, side="S"),
            sign(993200, 187000, 200, CLEAN_OTHER, side="S"),
        ],
    )
    assert session.scalar(select(func.count()).select_from(StreetSegmentSide)) == 2
    north = session.scalar(
        select(StreetSegmentSide).where(StreetSegmentSide.side == StreetSide.north)
    )
    south = session.scalar(
        select(StreetSegmentSide).where(StreetSegmentSide.side == StreetSide.south)
    )
    assert session.scalar(
        select(AspRule).where(AspRule.segment_side_id == north.id)
    ).days_of_week == [1, 4]
    assert session.scalar(
        select(AspRule).where(AspRule.segment_side_id == south.id)
    ).days_of_week == [2, 5]


def test_a_single_sign_yields_a_point_not_an_invented_line(session):
    """9% of block-sides carry one sign. A line from one point would assert an
    extent the data does not support."""
    report = load_signs(session, [sign(993000, 187000, 50)])
    assert report.point_only_sides == 1
    kind = session.execute(
        select(
            func.ST_GeometryType(
                func.cast(StreetSegmentSide.geom, __import__("geoalchemy2").Geometry())
            )
        )
    ).scalar()
    assert kind == "ST_Point"


def test_two_rules_on_one_block_side_are_both_kept(session):
    """A long block can carry two windows on different stretches. Keeping both
    over-warns on part of the block rather than missing a sweeper."""
    report = load_signs(
        session,
        [
            sign(993000, 187000, 50, CLEAN),
            sign(993400, 187020, 400, CLEAN_OTHER),
        ],
    )
    assert report.multi_rule_sides == 1
    assert session.scalar(select(func.count()).select_from(AspRule)) == 2


def test_reloading_does_not_duplicate_rules(session):
    rows = [sign(993000, 187000, 50), sign(993200, 187010, 200)]
    load_signs(session, rows)
    load_signs(session, rows)
    assert session.scalar(select(func.count()).select_from(StreetSegmentSide)) == 1
    assert session.scalar(select(func.count()).select_from(AspRule)) == 1


def test_a_rule_captured_from_the_sign_survives_a_reload(session):
    """The operator standing in front of the sign outranks a monthly dataset
    dump, so a reload must not delete what they recorded."""
    load_signs(session, [sign(993000, 187000, 50), sign(993200, 187010, 200)])
    seg = session.scalar(select(StreetSegmentSide))
    session.add(
        AspRule(
            segment_side_id=seg.id,
            days_of_week=[3],
            starts_at=__import__("datetime").time(9, 0),
            ends_at=__import__("datetime").time(10, 30),
            source=RuleSource.captured,
            confidence=1.0,
        )
    )
    session.commit()

    load_signs(session, [sign(993000, 187000, 50), sign(993200, 187010, 200)])
    sources = {r.source for r in session.scalars(select(AspRule))}
    assert RuleSource.captured in sources
    assert session.scalar(select(func.count()).select_from(AspRule)) == 2


def test_non_cleaning_signs_are_ignored(session):
    report = load_signs(
        session,
        [
            sign(993000, 187000, 50, "NO STANDING ANYTIME -->"),
            sign(993200, 187010, 200, "NO STANDING ANYTIME -->"),
        ],
    )
    assert report.rules_created == 0
    assert session.scalar(select(func.count()).select_from(StreetSegmentSide)) == 0


def test_a_row_with_no_side_of_street_is_reported_not_guessed(session):
    report = load_signs(session, [sign(993000, 187000, 50, side="")])
    assert report.signs_unparsed == 1
    assert "no usable block or side" in "".join(report.unparsed_reasons)


def test_the_line_extends_to_the_intersection_at_the_known_end():
    """The first sign records its distance from the intersection, so the near
    end is exact. Without it, a car parked by the corner matches nothing."""
    points = [(1000.0, 0.0), (1100.0, 0.0)]
    extended = _extend(points, first_distance_ft=60.0)
    assert extended[0] == (940.0, 0.0)  # 60 ft back towards the corner
    assert extended[-1][0] == pytest.approx(1100.0 + FORWARD_EXTENSION_FT)


def test_a_single_point_is_not_extended():
    assert _extend([(1000.0, 0.0)], first_distance_ft=60.0) == [(1000.0, 0.0)]
