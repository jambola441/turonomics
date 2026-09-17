"""Tests for the EZPass CSV parser using the real NY EZPass export format."""

from datetime import datetime

import pytest

from turonomics_api.parsing.ezpass import parse_ezpass_csv

# Minimal valid header row for convenience
_HDR = "Lane Txn ID,Tag/Plate #,Agency,Entry Plaza,Exit Plaza,Class,Date,Exit Time,Amount\n"


def row(
    txn: str = "10000001",
    tag_plate: str = " 00414500433",
    agency: str = "NYSTA",
    entry: str = "15",
    exit_: str = "19",
    cls: str = "2L",
    date: str = "12/29/2025",
    time: str = "05:13:32 PM",
    amount: str = "$-2.86",
) -> str:
    return f'"{txn}","{tag_plate}","{agency}","{entry}","{exit_}","{cls}","{date}","{time}","{amount}"\n'


class TestTransponderRows:
    def test_numeric_tag_becomes_transponder(self) -> None:
        tolls = parse_ezpass_csv(_HDR + row(tag_plate=" 00414500433"))
        assert tolls[0].transponder_id == "00414500433"
        assert tolls[0].license_plate is None

    def test_leading_spaces_stripped_from_transponder(self) -> None:
        tolls = parse_ezpass_csv(_HDR + row(tag_plate="   00414500433   "))
        assert tolls[0].transponder_id == "00414500433"

    def test_transponder_timestamp(self) -> None:
        tolls = parse_ezpass_csv(_HDR + row(date="12/29/2025", time="05:13:32 PM"))
        assert tolls[0].timestamp == datetime(2025, 12, 29, 17, 13, 32)

    def test_transponder_plaza_is_exit_plaza(self) -> None:
        tolls = parse_ezpass_csv(_HDR + row(exit_="19"))
        assert tolls[0].plaza == "19"

    def test_transponder_amount_is_positive(self) -> None:
        tolls = parse_ezpass_csv(_HDR + row(amount="$-2.86"))
        assert tolls[0].amount == 2.86


class TestPlateRows:
    def test_state_prefix_plate_becomes_license_plate(self) -> None:
        # "NY LZA7293" → state prefix stripped → "LZA7293"
        tolls = parse_ezpass_csv(_HDR + row(tag_plate="NY LZA7293"))
        assert tolls[0].license_plate == "LZA7293"
        assert tolls[0].transponder_id is None

    def test_plate_normalized_uppercase_no_spaces(self) -> None:
        # "nj abc 1234" → state prefix "nj " stripped → "ABC1234"
        tolls = parse_ezpass_csv(_HDR + row(tag_plate="nj abc 1234"))
        assert tolls[0].license_plate == "ABC1234"

    def test_plate_with_middle_dot_stripped(self) -> None:
        # "NY · LZA7293" → state prefix "NY " stripped → "· LZA7293" → dots removed → "LZA7293"
        tolls = parse_ezpass_csv(_HDR + row(tag_plate="NY · LZA7293"))
        assert tolls[0].license_plate == "LZA7293"

    def test_cbdtp_plate_row(self) -> None:
        tolls = parse_ezpass_csv(
            _HDR + row(tag_plate="NY LEH9892", agency="CBDTP", exit_="CRZ", amount="$-9.00")
        )
        assert tolls[0].license_plate == "LEH9892"
        assert tolls[0].amount == 9.00
        assert tolls[0].plaza == "CRZ"


class TestSkippedRows:
    def test_payment_row_skipped(self) -> None:
        payment = '""," ","","","PAYMENT","","12/29/2025","","$25.00"\n'
        tolls = parse_ezpass_csv(_HDR + payment)
        assert tolls == []

    def test_positive_amount_skipped(self) -> None:
        tolls = parse_ezpass_csv(_HDR + row(amount="$25.00"))
        assert tolls == []

    def test_zero_amount_skipped(self) -> None:
        tolls = parse_ezpass_csv(_HDR + row(amount="$0.00"))
        assert tolls == []

    def test_mixed_skips_and_valid(self) -> None:
        payment = '""," ","","","PAYMENT","","12/29/2025","","$25.00"\n'
        tolls = parse_ezpass_csv(_HDR + row() + payment + row(tag_plate="NY LZA7293"))
        assert len(tolls) == 2


class TestPlazaFallback:
    def test_empty_exit_plaza_falls_back_to_agency(self) -> None:
        tolls = parse_ezpass_csv(_HDR + row(exit_="", agency="NYSTA", entry="15"))
        assert tolls[0].plaza == "NYSTA"


class TestAmountParsing:
    def test_dollar_sign_stripped(self) -> None:
        tolls = parse_ezpass_csv(_HDR + row(amount="$-9.11"))
        assert tolls[0].amount == 9.11

    def test_no_dollar_sign(self) -> None:
        tolls = parse_ezpass_csv(_HDR + row(amount="-9.11"))
        assert tolls[0].amount == 9.11


class TestMissingColumns:
    def test_missing_tag_plate_raises(self) -> None:
        bad = "Lane Txn ID,Agency,Exit Plaza,Date,Exit Time,Amount\n"
        with pytest.raises(ValueError, match="missing required columns"):
            parse_ezpass_csv(bad)

    def test_missing_date_raises(self) -> None:
        bad = "Lane Txn ID,Tag/Plate #,Agency,Entry Plaza,Exit Plaza,Class,Exit Time,Amount\n"
        with pytest.raises(ValueError, match="missing required columns"):
            parse_ezpass_csv(bad)

    def test_empty_csv_raises(self) -> None:
        with pytest.raises(ValueError, match="empty"):
            parse_ezpass_csv("")


class TestBytesAndBOM:
    def test_accepts_bytes(self) -> None:
        tolls = parse_ezpass_csv((_HDR + row()).encode("utf-8"))
        assert len(tolls) == 1

    def test_accepts_utf8_bom(self) -> None:
        tolls = parse_ezpass_csv(("\ufeff" + _HDR + row()).encode("utf-8"))
        assert len(tolls) == 1


class TestRealWorldSample:
    """Parse the exact rows from examples/ezpass/transactions.csv."""

    SAMPLE = (
        "Lane Txn ID,Tag/Plate #,Agency,Entry Plaza,Exit Plaza,Class,Date,Exit Time,Amount\n"
        '"33237138399","NY LZA7293","MTAB&T","","RKB","31","12/31/2025","03:10:36 PM","$-9.11"\n'
        '"33232151931"," 00414500433","NYSTA","15","19","2L","12/29/2025","05:13:32 PM","$-2.86"\n'
        '"33229686477"," 00414500433","GSP","","BER","1","12/29/2025","03:08:32 PM","$-2.17"\n'
        '"33237855141","NY LEH9892","CBDTP","","CRZ","1","12/29/2025","02:14:27 PM","$-9.00"\n'
        '""," ","","","PAYMENT","","12/29/2025","","$25.00"\n'
    )

    def test_parses_four_tolls_skips_payment(self) -> None:
        tolls = parse_ezpass_csv(self.SAMPLE)
        assert len(tolls) == 4

    def test_plate_row_parsed_correctly(self) -> None:
        tolls = parse_ezpass_csv(self.SAMPLE)
        rbk = next(t for t in tolls if t.plaza == "RKB")
        assert rbk.license_plate == "LZA7293"
        assert rbk.transponder_id is None
        assert rbk.amount == 9.11
        assert rbk.timestamp == datetime(2025, 12, 31, 15, 10, 36)

    def test_transponder_row_parsed_correctly(self) -> None:
        tolls = parse_ezpass_csv(self.SAMPLE)
        nysta = next(t for t in tolls if t.plaza == "19")
        assert nysta.transponder_id == "00414500433"
        assert nysta.license_plate is None
        assert nysta.amount == 2.86
