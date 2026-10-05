"""Binding an EZPass transponder to the car it sits in.

A tag-read crossing names the tag and nothing else, so this mapping is the only
thing standing between a toll and the guest who owes it. It had no tests, which
is a poor state for the function that decides whose card gets charged.

The two ways it can go wrong are not symmetrical. Failing to bind leaves a
crossing unattributed, which is visible on the tolls page and costs nothing but
the chasing. Binding a tag to the wrong car bills one car's crossings to
another car's guests, silently and with a plausible total.
"""

from __future__ import annotations

import pytest

from turonomics_api.bootstrap import apply_tags, parse_tag_map
from turonomics_api.db.models import Vehicle

from .conftest import requires_db

pytestmark = requires_db


@pytest.fixture()
def fleet(session) -> dict[str, Vehicle]:
    cars = {
        "jimmy": Vehicle(nickname="Jimmy", make="Toyota", model="4Runner", year=2023,
                         plate="LEH9892"),
        "jolene": Vehicle(nickname="Jolene", make="Toyota", model="Corolla", year=2025,
                          plate="LWH4685"),
        "bubba": Vehicle(nickname="Bubba", make="Ford", model="Transit", year=2022,
                         plate="MJH2226", bouncie_nickname="The Van"),
    }
    session.add_all(cars.values())
    session.flush()
    return cars


# ---------------------------------------------------------------------------
# Reading the variable
# ---------------------------------------------------------------------------
def test_pairs_are_parsed_and_names_lowercased() -> None:
    assert parse_tag_map("Jimmy=00414500433,Jolene=00414500434") == {
        "jimmy": "00414500433",
        "jolene": "00414500434",
    }


def test_whitespace_and_empty_entries_are_tolerated() -> None:
    assert parse_tag_map(" Jimmy = 00414500433 , , Jerry=00415151710 ,") == {
        "jimmy": "00414500433",
        "jerry": "00415151710",
    }


def test_an_entry_with_no_equals_is_ignored() -> None:
    """Rather than binding half of it. A malformed variable should bind nothing
    from that entry, not a tag with an empty name."""
    assert parse_tag_map("Jimmy,Jolene=00414500434") == {"jolene": "00414500434"}


def test_an_empty_variable_is_an_empty_map() -> None:
    assert parse_tag_map("") == {}


def test_an_entry_with_no_name_is_ignored() -> None:
    """``"=00414500433"`` must bind nothing.

    A mutation that dropped this guard survived the first version of these
    tests: every case here happened to have a name on both sides, so nothing
    noticed a map keyed by the empty string — which would then match a vehicle
    whose nickname is blank.
    """
    assert parse_tag_map("=00414500433,Jolene=00414500434") == {"jolene": "00414500434"}


def test_an_entry_with_no_tag_is_ignored() -> None:
    assert parse_tag_map("Jimmy=,Jolene=00414500434") == {"jolene": "00414500434"}


# ---------------------------------------------------------------------------
# Binding
# ---------------------------------------------------------------------------
def test_a_tag_is_bound_by_nickname(session, fleet) -> None:
    changed = apply_tags(session, {"jimmy": "00414500433"})
    assert changed == ["Jimmy=00414500433"]
    assert fleet["jimmy"].ezpass_tag == "00414500433"


def test_a_tag_is_bound_by_the_bouncie_nickname_too(session, fleet) -> None:
    """The tracker's name for a car and the operator's are not always the same,
    and the variable should accept either."""
    assert apply_tags(session, {"the van": "00414500432"}) == ["Bubba=00414500432"]
    assert fleet["bubba"].ezpass_tag == "00414500432"


def test_binding_the_same_tag_again_changes_nothing(session, fleet) -> None:
    apply_tags(session, {"jimmy": "00414500433"})
    assert apply_tags(session, {"jimmy": "00414500433"}) == []


def test_an_unknown_nickname_binds_nothing(session, fleet) -> None:
    assert apply_tags(session, {"herbie": "00414500433"}) == []
    assert all(car.ezpass_tag is None for car in fleet.values())


# ---------------------------------------------------------------------------
# The dangerous case
# ---------------------------------------------------------------------------
def test_a_tag_another_car_holds_is_refused(session, fleet) -> None:
    """One transponder sits in one car.

    Two cars claiming it would bill one car's crossings to the other's guests,
    and the unique index would reject the write with a stack trace at boot.
    """
    apply_tags(session, {"jimmy": "00414500433"})
    changed = apply_tags(session, {"jolene": "00414500433"})
    assert changed == []
    assert fleet["jimmy"].ezpass_tag == "00414500433"
    assert fleet["jolene"].ezpass_tag is None


def test_refusing_one_car_does_not_stop_the_others(session, fleet) -> None:
    """A single bad entry must not throw away the rest of the variable."""
    apply_tags(session, {"jimmy": "00414500433"})
    changed = apply_tags(
        session, {"jolene": "00414500433", "bubba": "00414500432"}
    )
    assert changed == ["Bubba=00414500432"]


# ---------------------------------------------------------------------------
# Correcting one
# ---------------------------------------------------------------------------
# This used to be impossible. A car that had a tag kept it and the variable was
# ignored, so a typo could only be undone with database access and a replaced
# transponder could not be recorded at all — and transponders do get replaced.
def test_a_cars_tag_can_be_corrected(session, fleet) -> None:
    apply_tags(session, {"jerry": "00415151710", "jimmy": "00414500432"})
    changed = apply_tags(session, {"jimmy": "00414500433"})
    assert changed == ["Jimmy=00414500432->00414500433"]
    assert fleet["jimmy"].ezpass_tag == "00414500433"


def test_the_old_tag_is_released_for_another_car(session, fleet) -> None:
    """Otherwise correcting a tag that was on the wrong car would leave the
    right car unable to take it."""
    apply_tags(session, {"jimmy": "00414500434"})
    apply_tags(session, {"jimmy": "00414500433"})
    assert apply_tags(session, {"jolene": "00414500434"}) == ["Jolene=00414500434"]
    assert fleet["jolene"].ezpass_tag == "00414500434"
    assert fleet["jimmy"].ezpass_tag == "00414500433"


def test_two_cars_can_swap_tags_in_one_pass(session, fleet) -> None:
    """Stickers do go in the wrong windscreens, and the fix is a swap.

    This only works because every named car is cleared before any is assigned.
    A single pass could only do it if the database returned the losing car
    first, so the same variable could bind or refuse depending on row order.
    """
    apply_tags(session, {"jimmy": "00414500433", "jolene": "00414500434"})
    changed = apply_tags(session, {"jimmy": "00414500434", "jolene": "00414500433"})
    assert sorted(changed) == [
        "Jimmy=00414500433->00414500434",
        "Jolene=00414500434->00414500433",
    ]
    assert fleet["jimmy"].ezpass_tag == "00414500434"
    assert fleet["jolene"].ezpass_tag == "00414500433"


def test_a_freed_tag_is_taken_in_the_same_pass(session, fleet) -> None:
    """One call moves Jimmy off a tag and puts Jolene on it.

    A mutation that stopped releasing the old tag survived the first version of
    these tests, because they only ever reassigned across separate calls — and
    the holder lookup is rebuilt from the database each call, so nothing
    in-pass was being exercised at all.
    """
    apply_tags(session, {"jimmy": "00414500434"})
    changed = apply_tags(session, {"jimmy": "00414500433", "jolene": "00414500434"})
    assert sorted(changed) == [
        "Jimmy=00414500434->00414500433",
        "Jolene=00414500434",
    ]
    assert fleet["jolene"].ezpass_tag == "00414500434"


def test_two_named_cars_claiming_one_tag_bind_neither(session, fleet) -> None:
    """The variable cannot say which is right, so it changes nothing.

    Binding the first one reached would make the result depend on row order,
    and a wrong binding here bills one car's crossings to another's guests.
    """
    assert apply_tags(session, {"jimmy": "00414500433", "jolene": "00414500433"}) == []
    assert fleet["jimmy"].ezpass_tag is None
    assert fleet["jolene"].ezpass_tag is None


def test_a_tag_held_by_an_unnamed_car_is_not_taken(session, fleet) -> None:
    """Bubba is not in the variable, so his tag is not available to take.

    Moving it would unbind Bubba without saying so, and his crossings would
    start landing on nobody.
    """
    apply_tags(session, {"bubba": "00414500432"})
    assert apply_tags(session, {"jimmy": "00414500432"}) == []
    assert fleet["bubba"].ezpass_tag == "00414500432"
    assert fleet["jimmy"].ezpass_tag is None
