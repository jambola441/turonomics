"""Tests for the EZPass CSV parser using the real NY EZPass export format."""

from datetime import datetime
from pathlib import Path

import pytest

from turonomics_api.parsing.ezpass import parse_ezpass_csv

# Minimal valid header row for convenience
_HDR = "Lane Txn ID,Tag/Plate #,Agency,Entry Plaza,Exit Plaza,Class,Date,Exit Time,Amount\n"


def row(
    txn: str = "10000001",
    tag_plate: str = " 99900000111",
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
        tolls = parse_ezpass_csv(_HDR + row(tag_plate=" 99900000111"))
        assert tolls[0].transponder_id == "99900000111"
        assert tolls[0].license_plate is None

    def test_leading_spaces_stripped_from_transponder(self) -> None:
        tolls = parse_ezpass_csv(_HDR + row(tag_plate="   99900000111   "))
        assert tolls[0].transponder_id == "99900000111"

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
        '"33232151931"," 99900000111","NYSTA","15","19","2L","12/29/2025","05:13:32 PM","$-2.86"\n'
        '"33229686477"," 99900000111","GSP","","BER","1","12/29/2025","03:08:32 PM","$-2.17"\n'
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
        assert nysta.transponder_id == "99900000111"
        assert nysta.license_plate is None
        assert nysta.amount == 2.86


# ---------------------------------------------------------------------------
# The contract with the browser extension
# ---------------------------------------------------------------------------
def test_a_statement_scraped_from_the_website_parses() -> None:
    """The extension scrapes the activity page into this exact file.

    `examples/ezpass/scraped-from-page.csv` is generated by `toCsv` in
    `extension/src/tolls.ts` and asserted byte for byte by its test, so this is
    the other half of a contract between two languages: if the extension starts
    emitting something else, or this parser stops accepting it, one of the two
    fails. Without it, the scraper could quietly produce a file nothing reads —
    and nothing would notice until a month of tolls went missing.
    """
    fixture = Path(__file__).resolve().parents[2] / "examples/ezpass/scraped-from-page.csv"
    text = fixture.read_text()
    tolls = parse_ezpass_csv(text)

    # Asserted on the input, not just the output: "two tolls out" stays true if
    # the fixture quietly loses the payment row, and then nothing here is
    # testing the skip any more.
    assert len(text.strip().splitlines()) == 5, "header plus four rows"
    assert "PAYMENT" in text

    # Four rows in, two tolls out: the PAYMENT row and the positive-amount
    # credit are both skipped, which is the behaviour that makes a scraped page
    # safe to post repeatedly.
    assert len(tolls) == 2

    tag_read, plate_read = tolls
    assert tag_read.transponder_id == "99900000111"
    assert tag_read.license_plate is None
    assert tag_read.amount == pytest.approx(2.86)
    assert tag_read.timestamp == datetime(2025, 12, 29, 17, 13, 32)

    # The plaza carries a comma, so it has to have survived the quoting that
    # `toCsv` put around it and the CSV reader took off again.
    assert plate_read.license_plate == "LZA7293"
    assert plate_read.transponder_id is None
    assert plate_read.plaza == "Yonkers, NY"
    assert plate_read.amount == pytest.approx(9.11)


# ---------------------------------------------------------------------------
# The website's own date format
# ---------------------------------------------------------------------------
# The account-activity page renders one column reading "10/4/26 3:19 PM" where
# the download writes "12/29/2025" and "05:13:32 PM" in two. The first scrape
# of the real page was rejected on exactly this.


@pytest.mark.parametrize(
    ("date_cell", "expected"),
    [
        ("10/4/26 3:19 PM", datetime(2026, 10, 4, 15, 19)),
        ("10/4/26 3:19:07 PM", datetime(2026, 10, 4, 15, 19, 7)),
        ("10/4/26 15:19", datetime(2026, 10, 4, 15, 19)),
        ("10/4/26", datetime(2026, 10, 4)),
        # Midnight and noon are where a 12-hour clock goes wrong.
        ("1/1/26 12:00 AM", datetime(2026, 1, 1, 0, 0)),
        ("1/1/26 12:00 PM", datetime(2026, 1, 1, 12, 0)),
        # Still reads the download's format.
        ("12/29/2025 5:13:32 PM", datetime(2025, 12, 29, 17, 13, 32)),
    ],
)
def test_the_website_date_format_parses(date_cell: str, expected: datetime) -> None:
    header = "Tag/Plate #,Exit Plaza,Date,Amount\n"
    tolls = parse_ezpass_csv(header + f'" 99900000111","19","{date_cell}","$-2.86"\n')
    assert len(tolls) == 1
    assert tolls[0].timestamp == expected


def test_a_two_digit_year_is_not_read_as_the_year_26() -> None:
    """``%m/%d/%Y`` must not claim "10/4/26".

    If it did, the toll would be dated in the year 26 and the import would
    report success — a wrong answer rather than a refusal, which is the worse
    of the two by a long way. Python happens to refuse it; this pins that,
    because the format list now has both widths in it and the order is only
    safe while that holds.
    """
    tolls = parse_ezpass_csv(
        "Tag/Plate #,Exit Plaza,Date,Amount\n" '" 99900000111","19","10/4/26","$-2.86"\n'
    )
    assert tolls[0].timestamp.year == 2026


def test_a_date_in_no_known_format_still_names_itself() -> None:
    """The refusal has to carry the value, because that is how the next format
    gets added. A scraped page nobody here can open is only debuggable through
    this message."""
    with pytest.raises(ValueError, match=r"4th October '26"):
        parse_ezpass_csv(
            "Tag/Plate #,Exit Plaza,Date,Amount\n" "\" 99900000111\",\"19\",\"4th October '26\",\"$-2.86\"\n"
        )
