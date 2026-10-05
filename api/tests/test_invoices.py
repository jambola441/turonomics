"""What to bill each guest, and how long is left to ask.

Turo gives ninety days from the end of a trip to file a toll reimbursement.
It is the only deadline in this system that loses money by passing quietly: an
unbilled toll inside the window is a reminder, and the same toll outside it is
gone. So most of these tests are about the clock being right, and about an
expired invoice being reported as expired rather than counted as work.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from turonomics_api.db.models import Toll, Trip, TripSource, TripState, Vehicle

from .conftest import requires_db

pytestmark = requires_db

EASTERN = ZoneInfo("America/New_York")
HEADER = "Lane Txn ID,Tag/Plate #,Agency,Entry Plaza,Exit Plaza,Class,Date,Exit Time,Amount"


@pytest.fixture()
def jerry(session):
    car = Vehicle(nickname="Jerry", make="Toyota", model="Corolla", year=2025,
                  plate="LZA7293")
    session.add(car)
    session.flush()
    return car


def _trip(session, car, *, guest, starts, ends, source=TripSource.email, turo_id=None):
    trip = Trip(
        vehicle_id=car.id,
        turo_trip_id=turo_id,
        guest_name=guest,
        starts_at=starts,
        ends_at=ends,
        state=TripState.completed,
        source=source,
    )
    session.add(trip)
    session.flush()
    return trip


def _statement(*rows: str) -> bytes:
    return (HEADER + "\n" + "\n".join(rows) + "\n").encode()


def _row(txn: str, date: str, time: str, amount: str = "-9.11", plaza: str = "RKB") -> str:
    return f"{txn},NY LZA7293,MTAB&T,,{plaza},31,{date},{time},${amount}"


def _ago(days: int, hours: int = 0) -> datetime:
    """A whole second, so a floor division cannot drift over a day boundary.

    Two tests pinned exact values and failed by one: the microseconds between
    building a fixture and the request's own `now` are enough to turn 34 minutes
    into 2039 seconds, and -5 days into -6. Truncating here fixes the class
    rather than loosening the assertions.
    """
    return (datetime.now(UTC) - timedelta(days=days, hours=hours)).replace(microsecond=0)


# ---------------------------------------------------------------------------
# Grouping
# ---------------------------------------------------------------------------
def test_one_invoice_per_rental_with_a_line_each(api_client, monkeypatch, session, jerry):
    """A guest with several crossings is one bill, which is also how Turo's own
    form expects it."""
    monkeypatch.delenv("TOLLS_TOKEN", raising=False)
    _trip(session, jerry, guest="Dylan", starts=_ago(40), ends=_ago(39))
    date = (_ago(39) + timedelta(hours=-6)).astimezone(EASTERN)
    api_client.post("/api/tolls/import", files={"statement": ("a.csv", _statement(
        _row("1", date.strftime("%m/%d/%Y"), date.strftime("%I:%M:%S %p"), "-9.11"),
        _row("2", date.strftime("%m/%d/%Y"), date.strftime("%I:%M:%S %p"), "-6.94", "GWB"),
    ), "text/csv")})

    body = api_client.get("/api/invoices").json()
    assert len(body["invoices"]) == 1
    invoice = body["invoices"][0]
    assert invoice["guest_name"] == "Dylan"
    assert len(invoice["lines"]) == 2
    assert invoice["total_cents"] == 911 + 694
    assert body["billable_cents"] == 911 + 694


def test_a_crossing_nobody_owes_is_not_an_invoice(api_client, monkeypatch, session, jerry):
    monkeypatch.delenv("TOLLS_TOKEN", raising=False)
    monkeypatch.setenv("TOLL_OVERRUN_GRACE_MINUTES", "0")
    date = _ago(30).astimezone(EASTERN)
    api_client.post("/api/tolls/import", files={"statement": ("a.csv", _statement(
        _row("3", date.strftime("%m/%d/%Y"), date.strftime("%I:%M:%S %p")),
    ), "text/csv")})
    assert api_client.get("/api/invoices").json()["invoices"] == []


def test_a_crossing_already_billed_back_is_not_an_invoice(
    api_client, monkeypatch, session, jerry
):
    monkeypatch.delenv("TOLLS_TOKEN", raising=False)
    _trip(session, jerry, guest="Dylan", starts=_ago(40), ends=_ago(39))
    date = (_ago(39) + timedelta(hours=-6)).astimezone(EASTERN)
    api_client.post("/api/tolls/import", files={"statement": ("a.csv", _statement(
        _row("4", date.strftime("%m/%d/%Y"), date.strftime("%I:%M:%S %p")),
    ), "text/csv")})
    toll = session.scalars(select(Toll)).one()
    api_client.post(f"/api/tolls/{toll.id}/recovered")
    assert api_client.get("/api/invoices").json()["invoices"] == []
    # Still reachable when asked for, so a filed invoice can be looked at.
    assert len(api_client.get("/api/invoices?include_recovered=true").json()["invoices"]) == 1


# ---------------------------------------------------------------------------
# The clock
# ---------------------------------------------------------------------------
def test_the_deadline_is_ninety_days_from_the_trip_ending(
    api_client, monkeypatch, session, jerry
):
    monkeypatch.delenv("TOLLS_TOKEN", raising=False)
    trip = _trip(session, jerry, guest="Dylan", starts=_ago(31), ends=_ago(30))
    date = (_ago(30) + timedelta(hours=-6)).astimezone(EASTERN)
    api_client.post("/api/tolls/import", files={"statement": ("a.csv", _statement(
        _row("5", date.strftime("%m/%d/%Y"), date.strftime("%I:%M:%S %p")),
    ), "text/csv")})

    invoice = api_client.get("/api/invoices").json()["invoices"][0]
    assert invoice["days_left"] == 59, "ninety days from the end, thirty days ago"
    assert invoice["expired"] is False
    assert datetime.fromisoformat(invoice["file_by"]) == trip.ends_at + timedelta(days=90)


def test_an_invoice_past_the_window_is_reported_as_past_it(
    api_client, monkeypatch, session, jerry
):
    """Not hidden and not counted as collectable. A loss to acknowledge."""
    monkeypatch.delenv("TOLLS_TOKEN", raising=False)
    # Six hours clear of the boundary, so the floor is unambiguous.
    _trip(session, jerry, guest="Brandon", starts=_ago(96), ends=_ago(95, hours=-6))
    date = (_ago(95) + timedelta(hours=-6)).astimezone(EASTERN)
    api_client.post("/api/tolls/import", files={"statement": ("a.csv", _statement(
        _row("6", date.strftime("%m/%d/%Y"), date.strftime("%I:%M:%S %p"), "-16.79"),
    ), "text/csv")})

    body = api_client.get("/api/invoices").json()
    invoice = body["invoices"][0]
    assert invoice["expired"] is True
    assert invoice["days_left"] == -5
    assert body["expired_cents"] == 1679
    assert body["urgent_cents"] == 0, "an expired invoice is not this week's work"


def test_the_last_day_reads_zero_not_one(api_client, monkeypatch, session, jerry):
    """A day nearly gone is not a day in hand."""
    monkeypatch.delenv("TOLLS_TOKEN", raising=False)
    _trip(session, jerry, guest="Edge",
          starts=_ago(91), ends=datetime.now(UTC) - timedelta(days=90) + timedelta(hours=6))
    date = (_ago(90)).astimezone(EASTERN)
    api_client.post("/api/tolls/import", files={"statement": ("a.csv", _statement(
        _row("7", date.strftime("%m/%d/%Y"), date.strftime("%I:%M:%S %p")),
    ), "text/csv")})
    invoice = api_client.get("/api/invoices").json()["invoices"][0]
    assert invoice["days_left"] == 0
    assert invoice["expired"] is False


def test_soonest_deadline_first(api_client, monkeypatch, session, jerry):
    monkeypatch.delenv("TOLLS_TOKEN", raising=False)
    for days, guest, txn in ((80, "Soon", "8"), (10, "Later", "9")):
        _trip(session, jerry, guest=guest, starts=_ago(days + 1), ends=_ago(days))
        date = (_ago(days) + timedelta(hours=-6)).astimezone(EASTERN)
        api_client.post("/api/tolls/import", files={"statement": ("a.csv", _statement(
            _row(txn, date.strftime("%m/%d/%Y"), date.strftime("%I:%M:%S %p")),
        ), "text/csv")})
    names = [i["guest_name"] for i in api_client.get("/api/invoices").json()["invoices"]]
    assert names == ["Soon", "Later"]


def test_the_window_is_configurable(api_client, monkeypatch, session, jerry):
    """It is Turo's number, not this fleet's, so it should not need a diff."""
    monkeypatch.delenv("TOLLS_TOKEN", raising=False)
    monkeypatch.setenv("TOLL_FILING_WINDOW_DAYS", "30")
    _trip(session, jerry, guest="Dylan", starts=_ago(31), ends=_ago(30))
    date = (_ago(30) + timedelta(hours=-6)).astimezone(EASTERN)
    api_client.post("/api/tolls/import", files={"statement": ("a.csv", _statement(
        _row("10", date.strftime("%m/%d/%Y"), date.strftime("%I:%M:%S %p")),
    ), "text/csv")})
    body = api_client.get("/api/invoices").json()
    assert body["window_days"] == 30
    assert body["invoices"][0]["expired"] is True


# ---------------------------------------------------------------------------
# Off-platform
# ---------------------------------------------------------------------------
def test_an_off_platform_rental_has_no_turo_clock(api_client, monkeypatch, session, jerry):
    """There is nothing to file with Turo, so nothing expires — but the guest
    still owes it, so it is still an invoice."""
    monkeypatch.delenv("TOLLS_TOKEN", raising=False)
    _trip(session, jerry, guest="Priya", starts=_ago(200), ends=_ago(199),
          source=TripSource.manual)
    date = (_ago(199) + timedelta(hours=-6)).astimezone(EASTERN)
    api_client.post("/api/tolls/import", files={"statement": ("a.csv", _statement(
        _row("11", date.strftime("%m/%d/%Y"), date.strftime("%I:%M:%S %p")),
    ), "text/csv")})

    body = api_client.get("/api/invoices").json()
    invoice = body["invoices"][0]
    assert invoice["off_platform"] is True
    assert invoice["days_left"] is None
    assert invoice["file_by"] is None
    assert invoice["expired"] is False, "two hundred days old and still billable"
    assert body["off_platform_cents"] == 911
    assert body["expired_cents"] == 0


def test_off_platform_sorts_after_anything_with_a_deadline(
    api_client, monkeypatch, session, jerry
):
    """Nothing about it expires, so it must not push a Turo invoice with six
    days left down the page."""
    monkeypatch.delenv("TOLLS_TOKEN", raising=False)
    _trip(session, jerry, guest="Priya", starts=_ago(200), ends=_ago(199),
          source=TripSource.manual)
    _trip(session, jerry, guest="Dylan", starts=_ago(31), ends=_ago(30))
    for days, txn in ((199, "12"), (30, "13")):
        date = (_ago(days) + timedelta(hours=-6)).astimezone(EASTERN)
        api_client.post("/api/tolls/import", files={"statement": ("a.csv", _statement(
            _row(txn, date.strftime("%m/%d/%Y"), date.strftime("%I:%M:%S %p")),
        ), "text/csv")})
    names = [i["guest_name"] for i in api_client.get("/api/invoices").json()["invoices"]]
    assert names == ["Dylan", "Priya"]


def test_a_turo_invoice_carries_the_reservation_id(api_client, monkeypatch, session, jerry):
    """So the page can link to the place the invoice is actually filed."""
    monkeypatch.delenv("TOLLS_TOKEN", raising=False)
    _trip(session, jerry, guest="Dylan", starts=_ago(31), ends=_ago(30), turo_id="54958910")
    date = (_ago(30) + timedelta(hours=-6)).astimezone(EASTERN)
    api_client.post("/api/tolls/import", files={"statement": ("a.csv", _statement(
        _row("14", date.strftime("%m/%d/%Y"), date.strftime("%I:%M:%S %p")),
    ), "text/csv")})
    assert api_client.get("/api/invoices").json()["invoices"][0]["turo_trip_id"] == "54958910"


# ---------------------------------------------------------------------------
# Ticking one off
# ---------------------------------------------------------------------------
def test_ticking_an_invoice_marks_every_crossing_on_it(
    api_client, monkeypatch, session, jerry
):
    """One call, not twenty. Ticking crossing by crossing is how a row gets
    missed."""
    monkeypatch.delenv("TOLLS_TOKEN", raising=False)
    trip = _trip(session, jerry, guest="Dylan", starts=_ago(40), ends=_ago(39))
    date = (_ago(39) + timedelta(hours=-6)).astimezone(EASTERN)
    api_client.post("/api/tolls/import", files={"statement": ("a.csv", _statement(
        _row("15", date.strftime("%m/%d/%Y"), date.strftime("%I:%M:%S %p")),
        _row("16", date.strftime("%m/%d/%Y"), date.strftime("%I:%M:%S %p"), "-6.94", "GWB"),
    ), "text/csv")})

    response = api_client.post(f"/api/invoices/{trip.id}/recovered")
    assert response.status_code == 200
    assert len(response.json()["lines"]) == 2, "the invoice just ticked, not an empty one"
    assert api_client.get("/api/tolls").json()["unrecovered_cents"] == 0
    assert api_client.get("/api/invoices").json()["invoices"] == []


def test_ticking_an_invoice_can_be_undone(api_client, monkeypatch, session, jerry):
    monkeypatch.delenv("TOLLS_TOKEN", raising=False)
    trip = _trip(session, jerry, guest="Dylan", starts=_ago(40), ends=_ago(39))
    date = (_ago(39) + timedelta(hours=-6)).astimezone(EASTERN)
    api_client.post("/api/tolls/import", files={"statement": ("a.csv", _statement(
        _row("17", date.strftime("%m/%d/%Y"), date.strftime("%I:%M:%S %p")),
    ), "text/csv")})
    api_client.post(f"/api/invoices/{trip.id}/recovered")
    api_client.post(f"/api/invoices/{trip.id}/recovered?undo=true")
    assert len(api_client.get("/api/invoices").json()["invoices"]) == 1


def test_ticking_an_invoice_needs_the_token(api_client, monkeypatch, session, jerry):
    monkeypatch.delenv("TOLLS_TOKEN", raising=False)
    trip = _trip(session, jerry, guest="Dylan", starts=_ago(40), ends=_ago(39))
    date = (_ago(39) + timedelta(hours=-6)).astimezone(EASTERN)
    api_client.post("/api/tolls/import", files={"statement": ("a.csv", _statement(
        _row("18", date.strftime("%m/%d/%Y"), date.strftime("%I:%M:%S %p")),
    ), "text/csv")})
    monkeypatch.setenv("TOLLS_TOKEN", "letmein")
    assert api_client.post(f"/api/invoices/{trip.id}/recovered").status_code == 401
    assert api_client.get("/api/tolls").json()["unrecovered_cents"] == 911


def test_ticking_a_rental_with_no_crossings_is_a_404(api_client, monkeypatch, session, jerry):
    monkeypatch.delenv("TOLLS_TOKEN", raising=False)
    trip = _trip(session, jerry, guest="Nobody", starts=_ago(40), ends=_ago(39))
    assert api_client.post(f"/api/invoices/{trip.id}/recovered").status_code == 404


# ---------------------------------------------------------------------------
# The line a guest queries
# ---------------------------------------------------------------------------
def test_a_late_return_line_says_so(api_client, monkeypatch, session, jerry):
    """It is the line most likely to be disputed, so the invoice shows it."""
    monkeypatch.delenv("TOLLS_TOKEN", raising=False)
    monkeypatch.setenv("TOLL_OVERRUN_GRACE_MINUTES", "120")
    ends = _ago(30)
    _trip(session, jerry, guest="Dylan", starts=_ago(31), ends=ends)
    assert ends.microsecond == 0, "the overrun below is asserted to the second"
    date = (ends + timedelta(minutes=34)).astimezone(EASTERN)
    api_client.post("/api/tolls/import", files={"statement": ("a.csv", _statement(
        _row("19", date.strftime("%m/%d/%Y"), date.strftime("%I:%M:%S %p")),
    ), "text/csv")})
    line = api_client.get("/api/invoices").json()["invoices"][0]["lines"][0]
    assert line["overrun_seconds"] == 34 * 60
