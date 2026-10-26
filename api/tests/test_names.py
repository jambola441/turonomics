"""Undoing a name Turo sent double-encoded, and leaving every other name alone."""

from __future__ import annotations

import pytest

from turonomics_api.names import repair_mojibake


@pytest.mark.parametrize(
    ("damaged", "meant"),
    [
        ("Ä°lkay", "İlkay"),  # İ, the live case
        ("JosÃ©", "José"),  # é
        ("FranÃ§ois", "François"),  # ç
        ("BjÃ¶rk", "Björk"),  # ö
    ],
)
def test_utf8_read_as_one_byte_per_character_is_undone(damaged: str, meant: str) -> None:
    assert repair_mojibake(damaged) == meant


@pytest.mark.parametrize(
    "name",
    ["Shashi", "José", "İlkay", "Zoë", "Müller", "", "ALLISON", "王芳"],
)
def test_a_name_already_right_is_left_as_it_is(name: str) -> None:
    assert repair_mojibake(name) == name
