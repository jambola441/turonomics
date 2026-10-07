"""Tests for the plaza gazetteer: what a statement's plaza code means, and where.

The data was researched (sources on every row). What is tested here is that it
is used honestly — an ambiguous code is not resolved by guessing, a guessed
meaning needs the statement's agency to back it — and that no row is
obviously broken.
"""

from __future__ import annotations

import pytest

from turonomics_api.ingest.plazas import PLAZAS, USABLE, locate


def test_an_ambiguous_code_is_not_guessed_without_the_agency() -> None:
    """17 is the Thruway's Newburgh exit and the Turnpike's Secaucus one."""
    assert locate("17") is None


@pytest.mark.parametrize(
    ("agency", "where"),
    [("NYSTA", "Newburgh"), ("NJTP", "Secaucus"), ("NJ Turnpike", "Secaucus")],
)
def test_the_statements_agency_settles_an_ambiguous_code(agency: str, where: str) -> None:
    found = locate("17", agency)
    assert found is not None and where in found.name


def test_the_agency_is_matched_however_the_statement_spells_it() -> None:
    found = locate("rkb", "MTAB&T")
    assert found is not None and found.agency == "MTA"


def test_a_guessed_meaning_needs_the_agency_to_agree() -> None:
    """D95 as the Delaware Turnpike was a guess. A statement saying DelDOT
    confirms it; a statement saying nothing leaves it unplaced."""
    assert locate("D95") is None
    found = locate("D95", "DelDOT")
    assert found is not None and found.confidence == "low"


def test_an_agency_this_file_does_not_know_falls_back_to_the_code() -> None:
    found = locate("VNB", "Some New Name")
    assert found is not None and "Verrazzano" in found.name


def test_a_known_agency_with_a_code_it_does_not_have_is_not_moved_to_another_road() -> None:
    """NJTP has no interchange 24; that does not make it the Thruway's 24."""
    assert locate("24", "NJTP") is None
    assert locate("24") is not None


@pytest.mark.parametrize("code", ["TCN", "104", "109", "521", "522", "583"])
def test_the_codes_nobody_could_identify_are_not_placed(code: str) -> None:
    assert locate(code) is None


def test_every_row_is_sane() -> None:
    seen: set[tuple[str, str]] = set()
    for plaza in PLAZAS:
        key = (plaza.agency, plaza.code)
        assert key not in seen, f"{key} twice"
        seen.add(key)
        # The north-east, where E-ZPass New York's statements reach.
        assert 38.0 < plaza.lat < 45.5, plaza
        assert -77.5 < plaza.lon < -70.0, plaza
        assert plaza.confidence in USABLE | {"low"}, plaza
        assert plaza.source.startswith("https://"), plaza
        assert plaza.name, plaza
