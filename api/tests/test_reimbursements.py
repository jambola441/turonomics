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


# ---------------------------------------------------------------------------
# Line items
# ---------------------------------------------------------------------------
# Matching the total was not enough. Of eight charged invoices on the live
# account, not one total equalled the rental's crossings — a reimbursement
# bundles cleaning, fuel and damage onto the same invoice. The toll line is the
# part that can be reconciled.

ITEMISED_BODY = """Dylan invoice

Your guest has been charged.

View receipt (https://turo.com/reservation/54958910/receipt)

Reimbursement charges

Tolls - $15.55
Cleaning - $40.00

Total charge - $55.55
"""


def test_the_line_items_are_read() -> None:
    parsed = parse_invoice(
        "Dylan has been charged for your reimbursement invoice", ITEMISED_BODY
    )
    assert parsed is not None
    assert parsed.lines == (("Tolls", 1555), ("Cleaning", 4000))
    assert parsed.total_cents == 5555


def test_the_total_is_not_read_as_a_line_item() -> None:
    """It is the sum of the lines, not one of them."""
    parsed = parse_invoice(
        "Dylan has been charged for your reimbursement invoice", ITEMISED_BODY
    )
    assert parsed is not None
    assert all("total" not in label.lower() for label, _ in parsed.lines)


def test_the_toll_line_is_picked_out() -> None:
    parsed = parse_invoice(
        "Dylan has been charged for your reimbursement invoice", ITEMISED_BODY
    )
    assert parsed is not None and parsed.toll_cents == 1555


def test_an_invoice_with_no_toll_line_reports_none() -> None:
    """Entirely cleaning or damage. None rather than zero, so the caller can
    tell "no toll line" from "a toll line of nothing"."""
    body = ITEMISED_BODY.replace("Tolls - $15.55\n", "")
    parsed = parse_invoice("Dylan has been charged for your reimbursement invoice", body)
    assert parsed is not None and parsed.toll_cents is None


def test_a_singular_toll_label_is_recognised() -> None:
    body = ITEMISED_BODY.replace("Tolls -", "Toll -")
    parsed = parse_invoice("Dylan has been charged for your reimbursement invoice", body)
    assert parsed is not None and parsed.toll_cents == 1555


def test_a_wordier_toll_label_is_recognised() -> None:
    body = ITEMISED_BODY.replace("Tolls -", "Toll charges -")
    parsed = parse_invoice("Dylan has been charged for your reimbursement invoice", body)
    assert parsed is not None and parsed.toll_cents == 1555


def test_a_label_merely_containing_the_letters_is_not_a_toll_line() -> None:
    """Word boundaries, so "Tolled" or a plaza name does not become the toll
    line and write off the wrong amount.

    The label carries no other charge word on purpose. With "damage" in it this
    test passed with the word boundaries removed — the non-toll guard was
    refusing the line and the boundaries were doing nothing.
    """
    body = ITEMISED_BODY.replace("Tolls -", "Tollgate Lane repaint -")
    parsed = parse_invoice("Dylan has been charged for your reimbursement invoice", body)
    assert parsed is not None
    assert parsed.toll_cents is None, "a road name is not a toll charge"


@requires_db
def test_the_toll_line_is_what_gets_reconciled(
    api_client, monkeypatch, session, trip
) -> None:
    """A bundled invoice now ticks off its toll portion.

    $55.55 charged, of which $15.55 was tolls, against $15.55 of crossings.
    Comparing the total would have refused this — which is what every one of
    the eight live invoices did.
    """
    monkeypatch.delenv("TOLLS_TOKEN", raising=False)
    api_client.post("/api/tolls/import",
                    files={"statement": ("a.csv", _statement("-9.11", "-6.44"), "text/csv")})
    parsed = parse_invoice(
        "Dylan has been charged for your reimbursement invoice", ITEMISED_BODY
    )
    assert parsed is not None
    record_invoice(session, parsed, now=NOW)
    session.commit()

    assert api_client.get("/api/tolls").json()["unrecovered_cents"] == 0
    assert api_client.get("/api/invoices").json()["invoices"] == []


@requires_db
def test_a_toll_line_that_does_not_match_is_still_refused(
    api_client, monkeypatch, session, trip
) -> None:
    """Itemising does not mean guessing. A toll line of $9.00 against $15.55 of
    crossings is a part-payment or a different set, and writing the rest off
    would lose it."""
    monkeypatch.delenv("TOLLS_TOKEN", raising=False)
    api_client.post("/api/tolls/import",
                    files={"statement": ("a.csv", _statement("-9.11", "-6.44"), "text/csv")})
    body = ITEMISED_BODY.replace("Tolls - $15.55", "Tolls - $9.00")
    parsed = parse_invoice("Dylan has been charged for your reimbursement invoice", body)
    assert parsed is not None
    record_invoice(session, parsed, now=NOW)
    session.commit()
    assert api_client.get("/api/tolls").json()["unrecovered_cents"] == 1555


@requires_db
def test_the_lines_are_stored_so_the_labels_can_be_read(session, trip) -> None:
    """Turo's own words. The masked probe reports them as <NAME>, so storing
    them is the only way to find out what they are."""
    parsed = parse_invoice(
        "Dylan has been charged for your reimbursement invoice", ITEMISED_BODY
    )
    assert parsed is not None
    invoice = record_invoice(session, parsed, now=NOW)
    assert invoice.lines == [["Tolls", 1555], ["Cleaning", 4000]]
    assert invoice.toll_cents == 1555


@requires_db
def test_an_itemised_sighting_fills_in_what_an_earlier_one_lacked(session, trip) -> None:
    """The three notifications of one invoice do not all itemise, and they
    arrive in no particular order."""
    bare = parse_invoice("Dylan invoice", FILED_BODY)
    assert bare is not None and bare.toll_cents is None
    record_invoice(session, bare, now=NOW)

    itemised = parse_invoice(
        "Dylan has not responded to your reimbursement invoice",
        ITEMISED_BODY.replace("https://turo.com/reservation/54958910/receipt",
                              "https://turo.com/reservation/54958910/invoice-hub?invoiceId=abc123XY"),
    )
    assert itemised is not None and itemised.toll_cents == 1555
    invoice = record_invoice(session, itemised, now=NOW + timedelta(hours=1))
    assert invoice.toll_cents == 1555
    assert invoice.lines


def test_a_line_amount_is_not_truncated_by_a_cent() -> None:
    """284 of the first 5,900 amounts lose a cent to int(x * 100).

    $15.55 and $40.00 are not among them, which is why a mutation replacing
    round with truncation survived the first version of these tests. $2.01 is:
    int(2.01 * 100) is 200.
    """
    body = ITEMISED_BODY.replace("Tolls - $15.55", "Tolls - $2.01")
    parsed = parse_invoice("Dylan has been charged for your reimbursement invoice", body)
    assert parsed is not None
    assert parsed.toll_cents == 201
    assert ("Tolls", 201) in parsed.lines


# ---------------------------------------------------------------------------
# The other things on an invoice
# ---------------------------------------------------------------------------
# A reimbursement also charges for additional mileage, fuel and tickets. None
# of those is reconcilable here — nothing knows what the mileage should have
# been — but they have to parse, so the page can show what the invoice was for,
# and they must not be mistaken for the toll line.

BUNDLED_BODY = """Dylan invoice

Your guest has been charged.

View receipt (https://turo.com/reservation/54958910/receipt)

Reimbursement charges

Tolls - $15.55
Additional mileage (120 mi) - $42.00
Fuel - $38.75
Parking ticket - $65.00

Total charge - $161.30
"""


def test_mileage_fuel_and_tickets_all_parse() -> None:
    """Labels with digits and brackets included: "Additional mileage (120 mi)"
    did not match the first version of the line pattern at all."""
    parsed = parse_invoice(
        "Dylan has been charged for your reimbursement invoice", BUNDLED_BODY
    )
    assert parsed is not None
    assert parsed.lines == (
        ("Tolls", 1555),
        ("Additional mileage (120 mi)", 4200),
        ("Fuel", 3875),
        ("Parking ticket", 6500),
    )
    assert parsed.total_cents == 16130


def test_the_toll_line_is_still_found_among_them() -> None:
    parsed = parse_invoice(
        "Dylan has been charged for your reimbursement invoice", BUNDLED_BODY
    )
    assert parsed is not None and parsed.toll_cents == 1555


@pytest.mark.parametrize(
    "label",
    ["Additional mileage", "Fuel", "Gas", "Parking ticket", "Citation",
     "Cleaning", "Smoking", "Damage", "Late return fee"],
)
def test_no_other_charge_is_read_as_tolls(label: str) -> None:
    body = BUNDLED_BODY.replace("Tolls - $15.55\n", "").replace("Fuel -", f"{label} -")
    parsed = parse_invoice("Dylan has been charged for your reimbursement invoice", body)
    assert parsed is not None
    assert parsed.toll_cents is None, f"{label!r} was taken as a toll charge"


@pytest.mark.parametrize(
    "label",
    # One per word in the non-toll list, because each word's only job is to
    # refuse a label like this one. "distance" is here because Turo bills
    # mileage as "22 mi additional distance", so a combined line would say
    # "distance" and not "mileage".
    [
        "Tolls and fuel",
        "Tolls and gas",
        "Tolls and cleaning",
        "Tolls and additional distance",
        "Tolls and mileage",
        "Tolls and parking",
        "Tolls and damage",
        "Tolls and tickets",
        "Tolls and a citation",
        "Tolls and smoking",
        "Tolls and a violation",
        "Tolls and pet hair",
        "Tolls and delivery",
        "Tolls and overage",
        "Tolls and petrol",
    ],
)
def test_a_line_naming_tolls_and_something_else_is_refused(label: str) -> None:
    """"Tolls and fuel - $55.55" does not say what the toll share was.

    Taking the whole amount would write off the fuel as though the guest had
    paid it, so this falls back to the total and refuses.
    """
    body = BUNDLED_BODY.replace("Tolls - $15.55", f"{label} - $54.30")
    parsed = parse_invoice("Dylan has been charged for your reimbursement invoice", body)
    assert parsed is not None
    assert any(label in listed for listed, _ in parsed.lines), "still listed"
    assert parsed.toll_cents is None, "but not reconciled"


def test_two_toll_lines_are_refused_rather_than_improvised_on() -> None:
    """Not a shape seen in the wild, and not one to guess at: summing them
    assumes they are both this rental's, and taking the first assumes an order.
    """
    body = BUNDLED_BODY.replace("Fuel - $38.75", "Tolls - $38.75")
    parsed = parse_invoice("Dylan has been charged for your reimbursement invoice", body)
    assert parsed is not None and parsed.toll_cents is None


@requires_db
def test_a_bundled_invoice_reconciles_only_its_toll_line(
    api_client, monkeypatch, session, trip
) -> None:
    """$161.30 charged across four categories, $15.55 of it tolls, against
    $15.55 of crossings. The other $145.75 is not this ledger's business."""
    monkeypatch.delenv("TOLLS_TOKEN", raising=False)
    api_client.post("/api/tolls/import",
                    files={"statement": ("a.csv", _statement("-9.11", "-6.44"), "text/csv")})
    parsed = parse_invoice(
        "Dylan has been charged for your reimbursement invoice", BUNDLED_BODY
    )
    assert parsed is not None
    record_invoice(session, parsed, now=NOW)
    session.commit()
    assert api_client.get("/api/tolls").json()["unrecovered_cents"] == 0


@requires_db
def test_the_other_charges_are_shown_not_discarded(
    api_client, monkeypatch, session, trip
) -> None:
    """So a $161.30 invoice against $15.55 of tolls reads as a bundle rather
    than as a figure that makes no sense."""
    monkeypatch.delenv("TOLLS_TOKEN", raising=False)
    api_client.post("/api/tolls/import",
                    files={"statement": ("a.csv", _statement("-9.11"), "text/csv")})
    parsed = parse_invoice(
        "Dylan has been charged for your reimbursement invoice", BUNDLED_BODY
    )
    assert parsed is not None
    record_invoice(session, parsed, now=NOW)
    session.commit()
    row = api_client.get("/api/invoices").json()["invoices"][0]
    assert row["charged_but_different"] is True, "$15.55 of tolls against $9.11 of crossings"
    assert "Parking ticket $65.00" in row["charged_lines"]
    assert "Fuel $38.75" in row["charged_lines"]


# Each word in the other-charges list only ever acts when it sits beside
# "toll", so that is how each is tested. Without this the list was untested:
# the parametrised test above passes whether or not the word is in it, because
# "Fuel" does not match the toll pattern in the first place — which is why
# dropping "delivery" from the list survived mutation.
@pytest.mark.parametrize(
    "other",
    ["mileage", "miles", "fuel", "gas", "petrol", "ticket", "tickets", "citation",
     "citations", "violation", "violations", "cleaning", "smoking", "damage",
     "overage", "pet", "delivery", "parking"],
)
def test_tolls_combined_with_another_charge_is_refused(other: str) -> None:
    body = BUNDLED_BODY.replace("Tolls - $15.55", f"Tolls and {other} - $54.30")
    parsed = parse_invoice("Dylan has been charged for your reimbursement invoice", body)
    assert parsed is not None
    assert parsed.toll_cents is None, f"'Tolls and {other}' was reconciled as tolls"


@pytest.mark.parametrize("label", ["Toll fees", "Toll charges", "Toll reimbursement",
                                   "Tolls", "Toll"])
def test_a_plain_toll_label_is_still_reconciled(label: str) -> None:
    """The other side of the list, and the reason "fee" is not in it.

    "Toll fees - $15.55" is the toll line. Refusing it over a word would leave
    money uncollected, which is the mistake in the opposite direction from
    writing off money nobody paid.
    """
    body = BUNDLED_BODY.replace("Tolls - $15.55", f"{label} - $15.55")
    parsed = parse_invoice("Dylan has been charged for your reimbursement invoice", body)
    assert parsed is not None
    assert parsed.toll_cents == 1555, f"{label!r} was not recognised as the toll line"


# ---------------------------------------------------------------------------
# What the invoices actually look like
# ---------------------------------------------------------------------------
#
# Everything above this line was written against a guess at the format, and the
# guess passed. A probe of the real mailbox then read 149 reimbursement
# invoices and stored line items for *none* of them, because Turo writes the
# quantity before the label — "22 mi additional distance" — and the pattern
# required a leading letter. The heading is "Incidental charges", not the
# "Reimbursement charges" invented above, and each charge carries a sentence of
# explanation underneath it.

OBSERVED_BODY = """Reimbursement invoice

Your guest has been charged for the incidental charges below.

View invoice (https://turo.com/reservation/59077848/invoice-hub?invoiceId=INV-77)

59077848 Toyota Corolla

Filed by Marguerite

Incidental charges

22 mi additional distance - $11.00

Charged because the trip went over the distance included in this reservation.

7 tolls - $40.71

Total charge - $51.71

Learn more about reimbursements at Turo (https://help.turo.com/categories/360)
"""


def test_the_quantified_labels_turo_actually_writes_all_parse() -> None:
    parsed = parse_invoice(
        "Marguerite has been charged for your reimbursement invoice", OBSERVED_BODY
    )
    assert parsed is not None
    assert parsed.lines == (("22 mi additional distance", 1100), ("7 tolls", 4071))
    assert parsed.total_cents == 5171


def test_the_toll_line_is_found_despite_its_leading_count() -> None:
    """"7 tolls - $40.71" is the line the whole feature exists to read."""
    parsed = parse_invoice(
        "Marguerite has been charged for your reimbursement invoice", OBSERVED_BODY
    )
    assert parsed is not None and parsed.toll_cents == 4071


def test_the_sentence_under_a_charge_is_not_a_charge() -> None:
    """Each charge has an explanation below it. It ends in a full stop rather
    than an amount, which is what the end-of-line anchor is for."""
    parsed = parse_invoice(
        "Marguerite has been charged for your reimbursement invoice", OBSERVED_BODY
    )
    assert parsed is not None
    assert all("Charged because" not in label for label, _ in parsed.lines)


def test_a_quantified_distance_line_is_not_read_as_tolls() -> None:
    """Allowing a leading digit widens what counts as a label, so the guard
    that keeps a non-toll charge out of the toll line has to hold for these
    too."""
    body = OBSERVED_BODY.replace("7 tolls - $40.71", "3 gal fuel - $40.71")
    parsed = parse_invoice(
        "Marguerite has been charged for your reimbursement invoice", body
    )
    assert parsed is not None
    assert parsed.toll_cents is None


def test_a_sentence_that_happens_to_quote_an_amount_is_not_a_charge() -> None:
    """The anchor that keeps prose out had nothing pinning it.

    Dropping the end-of-line `$` from the line pattern passed the whole suite,
    because every line of prose in the fixtures was dash-free or amount-free.
    An invoice's explanatory sentences are neither by nature, and one that
    reads like a label would be invoiced as a charge the guest never incurred.
    """
    body = OBSERVED_BODY.replace(
        "Charged because the trip went over the distance included in this"
        " reservation.",
        "Charged because - $11.00 of distance was not included in this trip.",
    )
    parsed = parse_invoice(
        "Marguerite has been charged for your reimbursement invoice", body
    )
    assert parsed is not None
    assert all("Charged because" not in label for label, _ in parsed.lines)
    assert parsed.lines == (("22 mi additional distance", 1100), ("7 tolls", 4071))
