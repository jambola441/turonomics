"""The congestion relief zone's outline, from the MTA's open data."""

from __future__ import annotations

import pytest

from turonomics_api.ingest.crz import ZONE, entry, inside


@pytest.mark.parametrize(
    ("where", "point", "expected"),
    [
        ("Times Square", (40.7580, -73.9855), True),
        ("Wall Street", (40.7060, -74.0090), True),
        ("Central Park South, 59th St", (40.7650, -73.9760), True),
        ("W 61st St", (40.7720, -73.9850), False),
        ("the West Side Highway at W 14th, which is not charged", (40.7410, -74.0105), False),
        ("Downtown Brooklyn", (40.6930, -73.9900), False),
        ("Long Island City", (40.7440, -73.9480), False),
    ],
)
def test_the_zone_is_manhattan_below_60th_street(where, point, expected) -> None:
    assert inside(point) is expected, where


def test_the_outline_is_closed() -> None:
    assert ZONE[0] == ZONE[-1] and len(ZONE) > 100


def test_a_drive_in_from_outside_is_placed_on_the_edge() -> None:
    found = entry([(40.7740, -73.9820), (40.7650, -73.9840)])
    assert found is not None
    (lat, _lon), crossed = found
    assert crossed and 40.7650 < lat < 40.7740


def test_a_drive_that_stays_out_has_no_entry() -> None:
    assert entry([(40.7900, -73.9700), (40.7950, -73.9650)]) is None
    assert entry([]) is None
