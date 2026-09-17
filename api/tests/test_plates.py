"""Plate normalisation.

A plate mismatch between the registry and an EZPass export does not raise; it
just quietly matches nothing, and the month reads as "no tolls". So the rule
lives in one place and is pinned here.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import select

from turonomics_api.db.models import Vehicle
from turonomics_api.models import EZPassToll, TuroTrip
from turonomics_api.plates import normalize_optional_plate, normalize_plate


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("leh-9892", "LEH9892"),
        ("lwh 4685", "LWH4685"),
        ("LWH4685", "LWH4685"),
        ("  hzv-33 05 ", "HZV3305"),
        ("ny lmn9999", "NYLMN9999"),
    ],
)
def test_normalisation_is_stable_across_separators(raw: str, expected: str) -> None:
    assert normalize_plate(raw) == expected


def test_a_blank_optional_plate_is_none_not_empty_string() -> None:
    """EZPass exports carry blank plate fields on payment rows, and "" must not
    become a plate that matches another blank."""
    assert normalize_optional_plate("   ") is None
    assert normalize_optional_plate(" - ") is None
    assert normalize_optional_plate(None) is None


def test_the_registry_and_the_toll_parser_agree() -> None:
    """The join only works if both sides normalise identically."""
    typed_by_hand = "leh-9892"
    from_turo_export = TuroTrip(
        trip_id="T1", start="2026-09-01T00:00:00", end="2026-09-02T00:00:00",
        license_plate="LEH 9892",
    )
    from_ezpass = EZPassToll(
        timestamp="2026-09-01T12:00:00", plaza="GWB", amount=16.0,
        license_plate="NY LEH9892",
    )
    assert normalize_plate(typed_by_hand) == from_turo_export.license_plate
    assert from_ezpass.license_plate.endswith(from_turo_export.license_plate)


@pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL") and not os.environ.get("DATABASE_URL"),
    reason="no database configured",
)
def test_registry_normalises_on_write(session) -> None:
    session.add(Vehicle(nickname="Jimmy", make="Toyota", model="4-Runner", year=2023,
                        plate="leh-9892"))
    session.commit()
    stored = session.scalar(select(Vehicle).where(Vehicle.nickname == "Jimmy"))
    assert stored.plate == "LEH9892"
