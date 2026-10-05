"""Money already asked of a guest, so the page does not ask twice.

Turo sends three notifications about one reimbursement invoice — filed, not
responded, charged — and none of them is a trip email, so the trip parser
rightly refuses them and they used to be discarded as "not a trip".

Discarding them is expensive in one direction only. A crossing already charged
is not money waiting to be collected, and listing it as such invites a second
invoice for money the guest has already paid. That is a dispute, not income.

The other direction matters just as much: an invoice can cover cleaning or fuel
as easily as tolls, so one whose total does not match the crossings is recorded
and surfaced rather than written off.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from turonomics_api.db.models import (
    ReimbursementInvoice,
    Toll,
    Trip,
    TripSource,
    TripState,
    Vehicle,
)
from turonomics_api.gmail.parse import (
    INVOICE_CHARGED,
    INVOICE_FILED,
    INVOICE_UNANSWERED,
    classify_invoice,
    parse_invoice,
)
from turonomics_api.ingest.reimbursements import record_invoice, relink_invoices

from .conftest import requires_db

EASTERN = ZoneInfo("America/New_York")
NOW = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
HEADER = "Lane Txn ID,Tag/Plate #,Agency,Entry Plaza,Exit Plaza,Class,Date,Exit Time,Amount"


# ---------------------------------------------------------------------------
# Reading the notification
# ---------------------------------------------------------------------------
# Shapes taken from the live mailbox, by probe, masked. All three carry the
# reservation in a link and the amount in a "Total charge" line.

CHARGED_BODY = """Dylan invoice

Your guest has been charged.

View receipt (https://turo.com/reservation/54958910/receipt)

54958910 Toyota Corolla
Jolene by Turonomics

Toll charges

Total charge - $15.55

Learn more about reimbursements at Turo (https://help.turo.com/categories/123)
"""

FILED_BODY = """Dylan invoice

You sent an invoice.

View invoice (https://turo.com/reservation/54958910/invoice-hub?invoiceId=abc123XY)

Total charge - $15.55
"""


def test_the_three_subjects_are_told_apart() -> None:
    assert classify_invoice("Dylan has been charged for your reimbursement invoice") \
        == INVOICE_CHARGED
    assert classify_invoice("Dylan has not responded to your reimbursement invoice") \
        == INVOICE_UNANSWERED
    assert classify_invoice("Dylan invoice") == INVOICE_FILED
    assert classify_invoice("Dylan has sent you a message about your Corolla") is None


def test_not_responded_is_not_read_as_charged() -> None:
    """Its subject also contains "reimbursement invoice", so order matters."""
    assert classify_invoice("Dylan has not responded to your reimbursement invoice") \
        != INVOICE_CHARGED


def test_a_charged_notification_yields_reservation_and_amount() -> None:
    parsed = parse_invoice(
        "Dylan has been charged for your reimbursement invoice", CHARGED_BODY
    )
    assert parsed is not None
    assert parsed.state == INVOICE_CHARGED
    assert parsed.reservation_id == "54958910"
    assert parsed.total_cents == 1555
    assert parsed.guest_name == "Dylan"
    # The receipt link carries no invoice id, so the fingerprint falls back.
    assert parsed.turo_invoice_id is None
    assert parsed.fingerprint == "res:54958910:1555"


def test_a_filed_notification_yields_the_invoice_id() -> None:
    parsed = parse_invoice("Dylan invoice", FILED_BODY)
    assert parsed is not None
    assert parsed.turo_invoice_id == "abc123XY"
    assert parsed.fingerprint == "inv:abc123XY"


def test_an_amount_with_a_thousands_separator_parses() -> None:
    body = CHARGED_BODY.replace("$15.55", "$1,234.56")
    parsed = parse_invoice("Dylan has been charged for your reimbursement invoice", body)
    assert parsed is not None and parsed.total_cents == 123456


def test_an_amount_is_not_truncated_by_a_cent() -> None:
    """$2.01 is the amount int(2.01 * 100) turns into 200."""
    body = CHARGED_BODY.replace("$15.55", "$2.01")
    parsed = parse_invoice("Dylan has been charged for your reimbursement invoice", body)
    assert parsed is not None and parsed.total_cents == 201


def test_mail_that_is_not_an_invoice_is_not_parsed() -> None:
    assert parse_invoice("Your earnings are on the way!", "Total charge - $9.11") is None


def test_an_invoice_with_no_reservation_link_is_refused() -> None:
    """Rather than stored against nothing. Without the reservation there is no
    rental to tick off, so it would be a row nobody could use."""
    assert parse_invoice("Dylan invoice", "Total charge - $15.55") is None


def test_an_invoice_with_no_total_is_refused() -> None:
    assert parse_invoice(
        "Dylan invoice", "View invoice (https://turo.com/reservation/54958910/receipt)"
    ) is None


def test_the_reservation_can_come_from_a_localised_link() -> None:
    body = CHARGED_BODY.replace(
        "turo.com/reservation/54958910/receipt", "turo.com/us/en/reservation/54958910/receipt"
    )
    parsed = parse_invoice("Dylan has been charged for your reimbursement invoice", body)
    assert parsed is not None and parsed.reservation_id == "54958910"


# ---------------------------------------------------------------------------
# Storing it
# ---------------------------------------------------------------------------
pytestmark_db = requires_db


@pytest.fixture()
def jerry(session):
    car = Vehicle(nickname="Jerry", make="Toyota", model="Corolla", year=2025,
                  plate="LZA7293")
    session.add(car)
    session.flush()
    return car


@pytest.fixture()
def trip(session, jerry):
    row = Trip(
        vehicle_id=jerry.id,
        turo_trip_id="54958910",
        guest_name="Dylan",
        starts_at=NOW - timedelta(days=40),
        ends_at=NOW - timedelta(days=39),
        state=TripState.completed,
        source=TripSource.email,
    )
    session.add(row)
    session.flush()
    return row


def _statement(*amounts: str) -> bytes:
    rows = [
        f"{i},NY LZA7293,MTAB&T,,RKB,31,"
        f"{(NOW - timedelta(days=39, hours=6)).astimezone(EASTERN):%m/%d/%Y},"
        f"{(NOW - timedelta(days=39, hours=6)).astimezone(EASTERN):%I:%M:%S %p},${a}"
        for i, a in enumerate(amounts, start=700)
    ]
    return (HEADER + "\n" + "\n".join(rows) + "\n").encode()


@requires_db
def test_the_three_notifications_collapse_onto_one_invoice(session, trip) -> None:
    """Otherwise one invoice counts as three, and the total asked triples."""
    for subject, body in (
        ("Dylan invoice", FILED_BODY),
        ("Dylan has not responded to your reimbursement invoice", FILED_BODY),
        ("Dylan has been charged for your reimbursement invoice", FILED_BODY),
    ):
        parsed = parse_invoice(subject, body)
        assert parsed is not None
        record_invoice(session, parsed, now=NOW)
    rows = session.scalars(select(ReimbursementInvoice)).all()
    assert len(rows) == 1
    assert rows[0].state == INVOICE_CHARGED


@requires_db
def test_an_invoice_only_moves_forwards(session, trip) -> None:
    """Mail arrives out of order often enough that a "filed" notification can
    land after the "charged" one. Treating that as news would make collected
    money look outstanding again."""
    charged = parse_invoice("Dylan has been charged for your reimbursement invoice", FILED_BODY)
    filed = parse_invoice("Dylan invoice", FILED_BODY)
    assert charged and filed
    record_invoice(session, charged, now=NOW)
    record_invoice(session, filed, now=NOW + timedelta(hours=1))
    assert session.scalars(select(ReimbursementInvoice)).one().state == INVOICE_CHARGED


@requires_db
def test_a_charged_invoice_ticks_off_the_crossings_it_covers(
    api_client, monkeypatch, session, trip
) -> None:
    monkeypatch.delenv("TOLLS_TOKEN", raising=False)
    api_client.post("/api/tolls/import",
                    files={"statement": ("a.csv", _statement("-9.11", "-6.44"), "text/csv")})
    assert api_client.get("/api/tolls").json()["unrecovered_cents"] == 1555

    parsed = parse_invoice("Dylan has been charged for your reimbursement invoice", CHARGED_BODY)
    assert parsed is not None
    record_invoice(session, parsed, now=NOW)
    session.commit()

    assert api_client.get("/api/tolls").json()["unrecovered_cents"] == 0
    assert api_client.get("/api/invoices").json()["invoices"] == []


@requires_db
def test_an_invoice_only_filed_does_not_tick_anything_off(
    api_client, monkeypatch, session, trip
) -> None:
    """Asked for is not collected. Until the guest is charged the money is
    still outstanding."""
    monkeypatch.delenv("TOLLS_TOKEN", raising=False)
    api_client.post("/api/tolls/import",
                    files={"statement": ("a.csv", _statement("-9.11", "-6.44"), "text/csv")})
    parsed = parse_invoice("Dylan invoice", FILED_BODY)
    assert parsed is not None
    record_invoice(session, parsed, now=NOW)
    session.commit()

    body = api_client.get("/api/invoices").json()
    assert body["invoices"][0]["pending_cents"] == 1555
    assert body["invoices"][0]["charged_cents"] == 0
    assert api_client.get("/api/tolls").json()["unrecovered_cents"] == 1555


@requires_db
def test_a_charged_total_that_does_not_match_is_surfaced_not_written_off(
    api_client, monkeypatch, session, trip
) -> None:
    """An invoice can cover cleaning or fuel as easily as tolls.

    Writing the crossings off because the amounts are in the same ballpark
    would lose money nobody paid. So it is recorded, flagged, and left.
    """
    monkeypatch.delenv("TOLLS_TOKEN", raising=False)
    api_client.post("/api/tolls/import",
                    files={"statement": ("a.csv", _statement("-9.11"), "text/csv")})
    parsed = parse_invoice("Dylan has been charged for your reimbursement invoice", CHARGED_BODY)
    assert parsed is not None  # $15.55 against $9.11 of tolls
    record_invoice(session, parsed, now=NOW)
    session.commit()

    body = api_client.get("/api/invoices").json()
    assert len(body["invoices"]) == 1, "still outstanding"
    assert body["invoices"][0]["charged_but_different"] is True
    assert body["invoices"][0]["charged_cents"] == 1555
    assert body["needs_a_look_cents"] == 911
    assert api_client.get("/api/tolls").json()["unrecovered_cents"] == 911


@requires_db
def test_an_invoice_arriving_before_the_statement_is_applied_later(
    api_client, monkeypatch, session, trip
) -> None:
    """A reimbursement charged in August says nothing until the August
    statement is imported in October. The invoice is on file; importing is what
    gives it something to tick off."""
    monkeypatch.delenv("TOLLS_TOKEN", raising=False)
    parsed = parse_invoice("Dylan has been charged for your reimbursement invoice", CHARGED_BODY)
    assert parsed is not None
    record_invoice(session, parsed, now=NOW)
    session.commit()

    api_client.post("/api/tolls/import",
                    files={"statement": ("a.csv", _statement("-9.11", "-6.44"), "text/csv")})
    assert api_client.get("/api/tolls").json()["unrecovered_cents"] == 0


@requires_db
def test_an_invoice_for_a_reservation_this_fleet_has_no_trip_for_is_kept(
    session,
) -> None:
    """Stored unlinked rather than dropped: the trip mail may arrive later, and
    relinking is cheap."""
    body = CHARGED_BODY.replace("54958910", "99999999")
    parsed = parse_invoice("Dylan has been charged for your reimbursement invoice", body)
    assert parsed is not None
    invoice = record_invoice(session, parsed, now=NOW)
    assert invoice.trip_id is None
    assert session.scalars(select(ReimbursementInvoice)).one().reservation_id == "99999999"


@requires_db
def test_relinking_attaches_an_invoice_once_its_rental_exists(session, jerry) -> None:
    parsed = parse_invoice("Dylan has been charged for your reimbursement invoice", CHARGED_BODY)
    assert parsed is not None
    record_invoice(session, parsed, now=NOW)
    assert session.scalars(select(ReimbursementInvoice)).one().trip_id is None

    session.add(Trip(
        vehicle_id=jerry.id, turo_trip_id="54958910", guest_name="Dylan",
        starts_at=NOW - timedelta(days=40), ends_at=NOW - timedelta(days=39),
        state=TripState.completed, source=TripSource.email,
    ))
    session.flush()
    relink_invoices(session, now=NOW)
    assert session.scalars(select(ReimbursementInvoice)).one().trip_id is not None


@requires_db
def test_the_recovery_is_stamped_with_when_it_was_charged(
    api_client, monkeypatch, session, trip
) -> None:
    """Not with now. The ledger should say when the money arrived."""
    monkeypatch.delenv("TOLLS_TOKEN", raising=False)
    api_client.post("/api/tolls/import",
                    files={"statement": ("a.csv", _statement("-9.11", "-6.44"), "text/csv")})
    parsed = parse_invoice("Dylan has been charged for your reimbursement invoice", CHARGED_BODY)
    assert parsed is not None
    record_invoice(session, parsed, now=NOW)
    session.commit()
    for toll in session.scalars(select(Toll)):
        assert toll.recovered_at == NOW


@requires_db
def test_a_filed_invoice_leaves_the_crossings_alone_even_if_the_total_matches(
    api_client, monkeypatch, session, trip
) -> None:
    """The amounts agreeing is not payment.

    This is asserted on the crossings rather than on a figure, because the
    first version leaned on a filed invoice having no charged_at: stamping
    recovered_at with None left them outstanding by accident, so removing the
    state check changed nothing visible.
    """
    monkeypatch.delenv("TOLLS_TOKEN", raising=False)
    api_client.post("/api/tolls/import",
                    files={"statement": ("a.csv", _statement("-9.11", "-6.44"), "text/csv")})
    parsed = parse_invoice("Dylan invoice", FILED_BODY)
    assert parsed is not None and parsed.total_cents == 1555
    record_invoice(session, parsed, now=NOW)
    session.commit()
    for toll in session.scalars(select(Toll)):
        assert toll.recovered_at is None


@requires_db
def test_an_invoice_on_a_rental_with_no_crossings_is_not_a_mismatch(session, trip) -> None:
    """Most reimbursements are for cleaning or fuel on trips with no tolls.

    Reporting each as "did not match" would bury the handful worth looking at.
    """
    from turonomics_api.ingest.reimbursements import ReimbursementResult

    parsed = parse_invoice("Dylan has been charged for your reimbursement invoice", CHARGED_BODY)
    assert parsed is not None
    result = ReimbursementResult()
    record_invoice(session, parsed, now=NOW, result=result)
    assert result.unmatched_totals == []
    assert result.tolls_recovered == 0


@requires_db
def test_charged_at_is_set_only_when_charged(session, trip) -> None:
    """The invariant the recovery actually rests on.

    `_recover` stamps recovered_at from charged_at, so a crossing can only be
    written off once charged_at exists — and charged_at exists only once Turo
    says the guest was charged. Both guards in `_recover` survive mutation
    because of this, so this is the thing that has to be true.
    """
    filed = parse_invoice("Dylan invoice", FILED_BODY)
    assert filed is not None
    invoice = record_invoice(session, filed, now=NOW)
    assert invoice.state == INVOICE_FILED
    assert invoice.charged_at is None, "asked for is not collected"

    unanswered = parse_invoice(
        "Dylan has not responded to your reimbursement invoice", FILED_BODY
    )
    assert unanswered is not None
    invoice = record_invoice(session, unanswered, now=NOW + timedelta(days=1))
    assert invoice.charged_at is None, "still only asked for"

    charged = parse_invoice(
        "Dylan has been charged for your reimbursement invoice", FILED_BODY
    )
    assert charged is not None
    invoice = record_invoice(session, charged, now=NOW + timedelta(days=2))
    assert invoice.charged_at == NOW + timedelta(days=2)
