"""Tests for Turo's receipt: what reached the host, and settling by it.

The body shape is the one a probe of a real trip page reported
(`GET /api/reservation/<id>/receipt`), with its text masked; the labels and
amounts here are invented. What the tests pin is the narrow thing the receipt is
trusted for: an uncharged invoice whose toll line appears on the receipt for
exactly that amount was settled.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from datetime import timedelta as td
from typing import Any

import pytest

from turonomics_api.db.models import (
    ReimbursementInvoice,
    Toll,
    Trip,
    TripSource,
    TripState,
    Vehicle,
)
from turonomics_api.ingest.turo_receipt import (
    ReceiptResult,
    apply_receipt,
    is_receipt,
    money_cents,
    receipt_summary,
    toll_lines,
    total_earned_cents,
    wanted_receipts,
)

from .conftest import requires_db

NOW = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)
RES = "53459553"


def _line(label: str, value: Any, kind: str = "CURRENCY_POSITIVE") -> dict[str, Any]:
    return {
        "label": label, "labelHeader": None, "labelLink": None, "labelSubtextList": None,
        "receiptLineItemType": kind, "shouldDisplayDots": False, "value": value,
        "valueSubtext": None,
    }


def _receipt(*reimbursements: tuple[str, Any], total: Any = "$87.20") -> dict[str, Any]:
    return {
        "earningHeaderLabel": "Earnings",
        "earningLineItems": [
            _line("Turo fee", "-$9.00", "CURRENCY_NEGATIVE"),
            _line("Total earned", total, "TOTAL_EARNED"),
        ],
        "reimbursementHeaderLabel": "Reimbursements",
        "reimbursementLineItems": [_line(label, value) for label, value in reimbursements],
        "costDetailLineItems": [],
        "disputes": None,
        "refundLineItems": None,
    }


# ---------------------------------------------------------------------------
# Reading it
# ---------------------------------------------------------------------------
def test_money_is_read_whatever_way_turo_writes_it() -> None:
    assert money_cents("$30.07") == 3007
    assert money_cents("-$9.00") == -900
    assert money_cents("$1,234.50") == 123450
    assert money_cents("(30.07)") == -3007
    assert money_cents(30.07) == 3007
    assert money_cents({"amount": 30.07, "currencyCode": "USD"}) == 3007
    assert money_cents("n/a") is None
    assert money_cents(None) is None
    assert money_cents(True) is None


def test_only_reimbursement_lines_about_tolls_are_toll_lines() -> None:
    body = _receipt(("Tolls", "$30.07"), ("Additional distance", "$12.00"), ("Cleaning", "$20.00"))
    assert toll_lines(body) == [3007]


def test_a_negative_reimbursement_line_is_not_a_toll_line() -> None:
    assert toll_lines(_receipt(("Tolls", "-$30.07"))) == []


def test_total_earned_is_the_line_typed_as_such() -> None:
    assert total_earned_cents(_receipt(total="$87.20")) == 8720
    assert total_earned_cents({"earningLineItems": [_line("Fee", "-$9.00", "CURRENCY_NEGATIVE")]}) is None


def test_an_error_body_is_not_a_receipt() -> None:
    assert is_receipt(_receipt())
    assert not is_receipt({"error": "not found"})
    assert not is_receipt(None)
    assert not is_receipt([])


def test_the_summary_reads_like_the_receipt() -> None:
    summary = receipt_summary(_receipt(("Tolls", "$30.07")))
    assert summary == {
        "earnings": [("Turo fee", "-$9.00"), ("Total earned", "$87.20")],
        "reimbursements": [("Tolls", "$30.07")],
        "total_earned": "$87.20",
    }
    assert receipt_summary(None) is None


# ---------------------------------------------------------------------------
# Settling by it
# ---------------------------------------------------------------------------
@pytest.fixture()
def car(session):
    vehicle = Vehicle(nickname="Jimmy", make="Toyota", model="Corolla", year=2024, plate="LWH4685")
    session.add(vehicle)
    session.flush()
    return vehicle


@pytest.fixture()
def rental(session, car):
    trip = Trip(
        vehicle_id=car.id, turo_trip_id=RES, guest_name="Avery",
        starts_at=NOW - td(days=250), ends_at=NOW - td(days=248),
        state=TripState.completed, source=TripSource.email,
    )
    session.add(trip)
    session.flush()
    return trip


def _crossing(session, car, rental, cents):
    toll = Toll(
        vehicle_id=car.id, trip_id=rental.id, occurred_at=rental.ends_at - td(hours=4),
        plaza="CRZ", amount_cents=cents, fingerprint=f"t-{uuid.uuid4()}",
        imported_at=NOW - td(days=1),
    )
    session.add(toll)
    session.flush()
    return toll


def _invoice(session, rental, *, toll_cents=3007, state="filed", invoice_id="9001"):
    row = ReimbursementInvoice(
        fingerprint=f"inv:{invoice_id}", reservation_id=RES, turo_invoice_id=invoice_id,
        guest_name="Avery", state=state, total_cents=toll_cents or 0,
        lines=[["Tolls", toll_cents]] if toll_cents else [], toll_cents=toll_cents,
        trip_id=rental.id, first_seen_at=NOW - td(days=240), last_seen_at=NOW - td(days=240),
    )
    session.add(row)
    session.flush()
    return row


def _apply(session, body):
    result = ReceiptResult()
    apply_receipt(session, RES, body, now=NOW, result=result)
    return result


@requires_db
def test_a_receipt_showing_the_toll_line_paid_settles_the_invoice(session, car, rental) -> None:
    """Avery's case: an ACCEPTED invoice with a $30.07 toll line, never matched
    to a charged email, and four crossings imported long afterwards."""
    toll = _crossing(session, car, rental, 3007)
    row = _invoice(session, rental)

    result = _apply(session, _receipt(("Tolls", "$30.07")))

    assert row.state == "charged"
    assert row.charged_at == NOW
    assert toll.recovered_at == NOW, "and the toll line covers the crossings"
    assert result.charged == 1
    assert result.tolls_recovered == 1
    assert rental.turo_receipt is not None
    assert rental.receipt_synced_at == NOW


@requires_db
def test_a_different_amount_settles_nothing(session, car, rental) -> None:
    toll = _crossing(session, car, rental, 3007)
    row = _invoice(session, rental)

    result = _apply(session, _receipt(("Tolls", "$25.00")))

    assert row.state == "filed"
    assert toll.recovered_at is None
    assert result.charged == 0
    assert rental.turo_receipt is not None, "kept all the same"


@requires_db
def test_a_bigger_amount_settles_nothing_either(session, car, rental) -> None:
    """Exact, not "at least": a receipt toll line larger than the invoice's is
    some other charge, or two bundled, and which is not something to guess."""
    toll = _crossing(session, car, rental, 3007)
    row = _invoice(session, rental)

    result = _apply(session, _receipt(("Tolls", "$40.00")))

    assert row.state == "filed"
    assert toll.recovered_at is None
    assert result.charged == 0


@requires_db
def test_a_line_that_is_not_about_tolls_settles_nothing(session, car, rental) -> None:
    """The amount matching is not enough: $30.07 of cleaning is not $30.07 of
    tolls."""
    row = _invoice(session, rental)
    result = _apply(session, _receipt(("Cleaning", "$30.07")))
    assert row.state == "filed"
    assert result.charged == 0


@requires_db
def test_one_receipt_line_settles_one_invoice(session, car, rental) -> None:
    """Two invoices for the same amount and one line on the receipt: only one
    of them is shown paid."""
    first = _invoice(session, rental, invoice_id="9001")
    second = _invoice(session, rental, invoice_id="9002")

    result = _apply(session, _receipt(("Tolls", "$30.07")))

    assert result.charged == 1
    assert sorted([first.state, second.state]) == ["charged", "filed"]


@requires_db
def test_an_invoice_with_no_toll_line_is_never_settled_by_a_receipt(session, rental) -> None:
    row = _invoice(session, rental, toll_cents=None)
    result = _apply(session, _receipt(("Tolls", "$30.07")))
    assert row.state == "filed"
    assert result.charged == 0


@requires_db
def test_an_error_body_does_not_replace_a_receipt(session, rental) -> None:
    """A failed fetch is not "no receipt"."""
    _apply(session, _receipt(("Tolls", "$30.07")))
    kept = dict(rental.turo_receipt)

    result = _apply(session, {"error": "not found"})

    assert result.unreadable == 1
    assert rental.turo_receipt == kept


@requires_db
def test_a_receipt_for_a_trip_the_app_does_not_have_is_reported(session) -> None:
    result = ReceiptResult()
    apply_receipt(session, "99999999", _receipt(), now=NOW, result=result)
    assert result.unknown == ["99999999"]
    assert result.stored == 0


# ---------------------------------------------------------------------------
# Which receipts to read
# ---------------------------------------------------------------------------
@requires_db
def test_a_rental_with_an_unsettled_invoice_is_wanted_first(session, car, rental) -> None:
    _invoice(session, rental)
    assert wanted_receipts(session, now=NOW)[0] == RES


@requires_db
def test_a_receipt_read_today_is_not_wanted_again_yet(session, car, rental) -> None:
    _invoice(session, rental)
    rental.receipt_synced_at = NOW - td(hours=2)
    session.flush()
    assert RES not in wanted_receipts(session, now=NOW)

    rental.receipt_synced_at = NOW - td(days=2)
    session.flush()
    assert RES in wanted_receipts(session, now=NOW)


@requires_db
def test_a_settled_rental_is_only_read_the_first_time(session, car, rental) -> None:
    _invoice(session, rental, state="charged")
    assert RES in wanted_receipts(session, now=NOW), "never read, so backfilled"
    rental.receipt_synced_at = NOW - td(days=30)
    session.flush()
    assert RES not in wanted_receipts(session, now=NOW)


@requires_db
def test_never_read_trips_are_capped_per_pull(session, car) -> None:
    """A long history fills in over several pulls, not as one burst of requests
    to Turo."""
    for i in range(25):
        session.add(
            Trip(
                vehicle_id=car.id, turo_trip_id=f"7000{i:04d}", guest_name="G",
                starts_at=NOW - td(days=100 + i), ends_at=NOW - td(days=99 + i),
                state=TripState.completed, source=TripSource.email,
            )
        )
    session.flush()
    assert len(wanted_receipts(session, now=NOW)) == 10


# ---------------------------------------------------------------------------
# Over HTTP
# ---------------------------------------------------------------------------
@requires_db
def test_posting_receipts_settles_and_the_trip_view_shows_them(
    api_client, session, car, rental
) -> None:
    _crossing(session, car, rental, 3007)
    row = _invoice(session, rental)
    session.commit()

    out = api_client.post(
        "/api/turo/receipts",
        json={"items": [{"reservation_id": RES, "receipt": _receipt(("Tolls", "$30.07"))}]},
    ).json()

    assert out["stored"] == 1
    assert out["charged"] == 1
    assert out["tolls_recovered"] == 1
    assert out["settled"] == [f"{RES}: $30.07 of tolls is on the receipt"]

    session.refresh(row)
    assert row.state == "charged"
    view = api_client.get(f"/api/trips/{rental.id}/view").json()
    assert view["receipt"]["total_earned"] == "$87.20"
    assert view["receipt"]["reimbursements"] == [{"label": "Tolls", "value": "$30.07"}]


@requires_db
def test_the_wanted_list_carries_the_receipts_to_read(api_client, session, car, rental) -> None:
    _invoice(session, rental)
    session.commit()
    out = api_client.get("/api/turo/wanted").json()
    assert RES in out["receipts"]
    assert out["receipt_path"] == "/api/reservation/{id}/receipt"


@requires_db
def test_one_receipt_line_for_two_invoices_settles_both(session, car, rental) -> None:
    """44623088: invoices for $27.13 (paid) and $40.69 (ACCEPTED), and one
    receipt line of $67.82 — the two together. Neither matched it alone."""
    tolls = [_crossing(session, car, rental, c) for c in (2713, 4069)]
    paid = _invoice(session, rental, toll_cents=2713, state="charged", invoice_id="9001")
    paid.charged_at = NOW - td(days=200)
    accepted = _invoice(session, rental, toll_cents=4069, invoice_id="9002")

    result = _apply(session, _receipt(("Tolls", "$67.82")))

    assert accepted.state == "charged"
    assert result.charged == 1
    assert all(t.recovered_at is not None for t in tolls)


@requires_db
def test_a_receipt_total_short_of_the_invoices_settles_neither(session, car, rental) -> None:
    _invoice(session, rental, toll_cents=2713, state="charged", invoice_id="9001")
    accepted = _invoice(session, rental, toll_cents=4069, invoice_id="9002")
    _apply(session, _receipt(("Tolls", "$60.00")))
    assert accepted.state == "filed"


@requires_db
def test_rematch_applies_a_stored_receipt_again(
    api_client, monkeypatch, session, car, rental
) -> None:
    """A rule learned after the receipt was read applies on Re-match, not only
    on the next read."""
    monkeypatch.delenv("TOLLS_TOKEN", raising=False)
    _invoice(session, rental, toll_cents=2713, state="charged", invoice_id="9001")
    accepted = _invoice(session, rental, toll_cents=4069, invoice_id="9002")
    rental.turo_receipt = _receipt(("Tolls", "$67.82"))
    session.commit()

    assert api_client.post("/api/tolls/rematch").status_code == 200
    session.refresh(accepted)
    assert accepted.state == "charged"


@requires_db
def test_a_refuelling_invoice_is_settled_when_every_line_is_on_the_receipt(
    session, car, rental
) -> None:
    """Ian Alexander's: $13.36 refueling and a $10.00 convenience fee, ACCEPTED
    on Turo; his receipt reads "Gas was not replaced $13.36" and "Convenience
    fee $10.00". Turo words them differently, so it is the amounts."""
    refuel = _invoice(session, rental, toll_cents=None, invoice_id="9101")
    refuel.lines = [["Refueling", 1336], ["Refueling convenience fee", 1000]]
    refuel.total_cents = 2336
    _apply(session, _receipt(("Tolls were incurred", "$34.66"), ("Gas was not replaced", "$13.36"),
                             ("Convenience fee", "$10.00"), ("Reimbursement total", "$58.02")))
    assert refuel.state == "charged"


@requires_db
def test_one_line_matching_is_not_enough(session, car, rental) -> None:
    refuel = _invoice(session, rental, toll_cents=None, invoice_id="9101")
    refuel.lines = [["Refueling", 1336], ["Refueling convenience fee", 1000]]
    _apply(session, _receipt(("Gas was not replaced", "$13.36")))
    assert refuel.state == "filed"


@requires_db
def test_a_toll_line_on_the_receipt_does_not_pay_another_kind_of_invoice(
    session, car, rental
) -> None:
    """$30.07 of tolls on the receipt is not $30.07 of cleaning."""
    cleaning = _invoice(session, rental, toll_cents=None, invoice_id="9102")
    cleaning.lines = [["Cleaning", 3007]]
    _apply(session, _receipt(("Tolls", "$30.07")))
    assert cleaning.state == "filed"


@requires_db
def test_a_receipt_line_pays_one_invoice_line_once(session, car, rental) -> None:
    first = _invoice(session, rental, toll_cents=None, invoice_id="9103")
    first.lines = [["Cleaning", 2000]]
    second = _invoice(session, rental, toll_cents=None, invoice_id="9104")
    second.lines = [["Cleaning", 2000]]
    _apply(session, _receipt(("Cleaning fee", "$20.00")))
    assert sorted([first.state, second.state]) == ["charged", "filed"]


@requires_db
def test_the_receipts_own_total_pays_nothing(session, car, rental) -> None:
    """"Reimbursement total" repeats the lines above it; on a receipt whose
    other line is tolls, it would otherwise pay a $30.07 cleaning invoice."""
    cleaning = _invoice(session, rental, toll_cents=None, invoice_id="9105")
    cleaning.lines = [["Cleaning", 3007]]
    _apply(session, _receipt(("Tolls", "$30.07"), ("Reimbursement total", "$30.07")))
    assert cleaning.state == "filed"
