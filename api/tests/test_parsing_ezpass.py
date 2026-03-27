import pytest

from turonomics_api.parsing.ezpass import parse_ezpass_csv
from datetime import datetime


def test_parse_valid_csv() -> None:
    csv_data = (
        "Date,Time,Location,Debit,Tag,License Plate\n"
        "06/15/2024,09:30:00,Verrazano Bridge,19.00,E-ZPass-123456,ABC1234\n"
    )
    tolls = parse_ezpass_csv(csv_data)
    assert len(tolls) == 1
    t = tolls[0]
    assert t.timestamp == datetime(2024, 6, 15, 9, 30, 0)
    assert t.plaza == "Verrazano Bridge"
    assert t.amount == 19.0
    assert t.transponder_id == "E-ZPass-123456"
    assert t.license_plate == "ABC1234"


def test_zero_amount_rows_skipped() -> None:
    csv_data = (
        "Date,Time,Location,Debit,Tag\n"
        "06/15/2024,09:30:00,Verrazano Bridge,0.00,E-ZPass-123456\n"
        "06/15/2024,10:00:00,Goethals Bridge,16.00,E-ZPass-123456\n"
    )
    tolls = parse_ezpass_csv(csv_data)
    assert len(tolls) == 1
    assert tolls[0].plaza == "Goethals Bridge"


def test_dollar_sign_in_amount() -> None:
    csv_data = (
        "Date,Time,Location,Debit,Tag\n"
        "06/15/2024,09:30:00,Verrazano Bridge,$19.00,E-ZPass-123456\n"
    )
    tolls = parse_ezpass_csv(csv_data)
    assert tolls[0].amount == 19.0


def test_missing_required_column_raises() -> None:
    csv_data = "Date,Location,Debit\n06/15/2024,Verrazano Bridge,19.00\n"
    with pytest.raises(ValueError, match="missing required columns"):
        parse_ezpass_csv(csv_data)


def test_no_license_plate_column_ok() -> None:
    """License plate column is optional."""
    csv_data = (
        "Date,Time,Location,Debit,Tag\n"
        "06/15/2024,09:30:00,Verrazano Bridge,19.00,E-ZPass-123456\n"
    )
    tolls = parse_ezpass_csv(csv_data)
    assert tolls[0].license_plate is None


def test_plate_normalized() -> None:
    csv_data = (
        "Date,Time,Location,Debit,Tag,License Plate\n"
        "06/15/2024,09:30:00,Verrazano Bridge,19.00,E-ZPass-123456,abc 1234\n"
    )
    tolls = parse_ezpass_csv(csv_data)
    assert tolls[0].license_plate == "ABC1234"


def test_multiple_rows() -> None:
    csv_data = (
        "Date,Time,Location,Debit,Tag\n"
        "06/01/2024,14:00:00,Verrazano Bridge,19.00,E-ZPass-111\n"
        "06/02/2024,08:30:00,Goethals Bridge,16.00,E-ZPass-222\n"
        "06/03/2024,11:00:00,Lincoln Tunnel,17.00,E-ZPass-333\n"
    )
    tolls = parse_ezpass_csv(csv_data)
    assert len(tolls) == 3


def test_accepts_bytes_with_bom() -> None:
    csv_data = (
        "\ufeffDate,Time,Location,Debit,Tag\n"
        "06/15/2024,09:30:00,Verrazano Bridge,19.00,E-ZPass-123456\n"
    ).encode("utf-8")
    tolls = parse_ezpass_csv(csv_data)
    assert len(tolls) == 1
