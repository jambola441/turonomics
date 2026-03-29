from datetime import datetime

import pytest

from turonomics_api.parsing.turo import parse_turo_csv


def test_parse_valid_csv() -> None:
    csv_data = (
        "trip_id,start_time,end_time,license_plate\n"
        "T001,2024-06-01T09:00:00,2024-06-03T18:00:00,ABC 1234\n"
    )
    trips = parse_turo_csv(csv_data)
    assert len(trips) == 1
    t = trips[0]
    assert t.trip_id == "T001"
    assert t.start == datetime(2024, 6, 1, 9, 0, 0)
    assert t.end == datetime(2024, 6, 3, 18, 0, 0)
    assert t.license_plate == "ABC1234"  # spaces stripped


def test_plate_normalized_uppercase() -> None:
    csv_data = (
        "trip_id,start_time,end_time,license_plate\n"
        "T001,2024-06-01T09:00:00,2024-06-03T18:00:00,abc1234\n"
    )
    trips = parse_turo_csv(csv_data)
    assert trips[0].license_plate == "ABC1234"


def test_multiple_trips() -> None:
    csv_data = (
        "trip_id,start_time,end_time,license_plate\n"
        "T001,2024-06-01T09:00:00,2024-06-03T18:00:00,ABC1234\n"
        "T002,2024-06-05T08:00:00,2024-06-07T20:00:00,XYZ5678\n"
    )
    trips = parse_turo_csv(csv_data)
    assert len(trips) == 2


def test_missing_required_column_raises() -> None:
    csv_data = "trip_id,start_time,license_plate\nT001,2024-06-01T09:00:00,ABC1234\n"
    with pytest.raises(ValueError, match="missing required columns"):
        parse_turo_csv(csv_data)


def test_empty_csv_raises() -> None:
    with pytest.raises(ValueError, match="empty"):
        parse_turo_csv("")


def test_end_before_start_raises() -> None:
    csv_data = (
        "trip_id,start_time,end_time,license_plate\n"
        "T001,2024-06-03T18:00:00,2024-06-01T09:00:00,ABC1234\n"
    )
    with pytest.raises(ValueError, match="end_time"):
        parse_turo_csv(csv_data)


def test_accepts_bytes_with_bom() -> None:
    csv_data = (
        "\ufefftrip_id,start_time,end_time,license_plate\n"
        "T001,2024-06-01T09:00:00,2024-06-03T18:00:00,ABC1234\n"
    ).encode("utf-8")
    trips = parse_turo_csv(csv_data)
    assert len(trips) == 1


def test_alternate_column_names() -> None:
    """Parser should accept 'start' instead of 'start_time', etc."""
    csv_data = (
        "id,start,end,plate\n"
        "T001,2024-06-01T09:00:00,2024-06-03T18:00:00,ABC1234\n"
    )
    trips = parse_turo_csv(csv_data)
    assert trips[0].trip_id == "T001"
    assert trips[0].license_plate == "ABC1234"


def test_utc_z_timestamps_converted_to_eastern() -> None:
    """Turo exports UTC timestamps with Z suffix; they must be converted to
    Eastern so they compare correctly against EZPass local timestamps.
    2025-12-29T18:00:00.000Z UTC = 2025-12-29T13:00:00 Eastern (UTC-5 in Dec)."""
    csv_data = (
        "trip_id,start_time,end_time,license_plate\n"
        "T001,2025-12-29T18:00:00.000Z,2026-01-02T18:00:00.000Z,LEH9892\n"
    )
    trips = parse_turo_csv(csv_data)
    assert trips[0].start == datetime(2025, 12, 29, 13, 0, 0)
    assert trips[0].end == datetime(2026, 1, 2, 13, 0, 0)


def test_missing_trip_id_gets_default() -> None:
    csv_data = (
        "start_time,end_time,license_plate\n"
        "2024-06-01T09:00:00,2024-06-03T18:00:00,ABC1234\n"
    )
    trips = parse_turo_csv(csv_data)
    assert trips[0].trip_id.startswith("row-")
