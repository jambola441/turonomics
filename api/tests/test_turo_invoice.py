"""Tests for reading a reimbursement's breakdown off Turo's invoice page.

The case this exists for is 59077848: $140.40 charged through an email that
did not itemise, beside $40.71 of crossings. Until Turo says what the $140.40
was, those crossings cannot be filed for — and once it says, they either can
be, or have already been paid.

Body shapes are the ones in docs/design/03-turo-api.md, observed on the live
account with values masked; the values here are invented.
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
from turonomics_api.ingest.turo_invoice import (
    TuroInvoiceResult,
    _amounts,
    apply_turo_invoice,
    hub_to_read,
    parse_hub,
    parse_turo_invoice,
    wanted_invoices,
)

from .conftest import requires_db

NOW = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
ENDS = NOW - td(days=20)
RES = "59077848"


def _body(*items: tuple[str, str, float], total: float | None = None, **extra: Any) -> dict:
    """An invoice-page body, shaped as observed."""
    return {
        "invoiceId": 113672232,
        "reimbursementId": 5550001,
        "reimbursementStatus": "REIMBURSEMENT_PAID_BY_GUEST_EXAMPLE",
        "title": "Reimbursement",
        "lineItems": [
            {
                "id": index,
                "type": kind,
                "title": title,
                "description": "",
                "total": {"amount": amount, "currencyCode": "USD"},
                "reimbursementLineItems": [],
                "evidenceImagesResponse": {"images": []},
            }
            for index, (kind, title, amount) in enumerate(items)
        ],
        "fees": [],
        "total": {
            "amount": total if total is not None else sum(a for _, _, a in items),
            "currencyCode": "USD",
        },
        **extra,
    }


AUSTIN = _body(
    ("TOLL_REIMBURSEMENT", "Tolls", 40.71),
    ("ADDITIONAL_DISTANCE", "Additional distance", 99.69),
)


# ---------------------------------------------------------------------------
# Reading the body
# ---------------------------------------------------------------------------


def test_the_toll_share_comes_from_the_type_not_the_title() -> None:
    parsed = parse_turo_invoice(RES, AUSTIN)
    assert parsed is not None
    assert parsed.total_cents == 14040
    assert parsed.toll_cents == 4071
    assert [line.title for line in parsed.lines] == ["Tolls", "Additional distance"]


def test_a_toll_line_titled_oddly_is_still_tolls() -> None:
    """The point of the enum. A title is Turo's copy and can say anything."""
    parsed = parse_turo_invoice(RES, _body(("TOLL_REIMBURSEMENT", "E-ZPass charges", 12.5)))
    assert parsed is not None and parsed.toll_cents == 1250


def test_a_title_mentioning_tolls_on_another_type_is_not_tolls() -> None:
    """The other half: a ticket line whose copy happens to say "toll" is a
    ticket. Taking it as tolls would tick crossings off against a fine."""
    parsed = parse_turo_invoice(RES, _body(("TICKET_REIMBURSEMENT", "Toll-road ticket", 50.0)))
    assert parsed is not None and parsed.toll_cents is None


def test_an_invoice_with_no_toll_line_charged_no_tolls() -> None:
    parsed = parse_turo_invoice(RES, _body(("TICKET_REIMBURSEMENT", "Tickets", 50.0)))
    assert parsed is not None and parsed.toll_cents is None


def test_two_toll_lines_add_up() -> None:
    parsed = parse_turo_invoice(
        RES,
        _body(("TOLL_REIMBURSEMENT", "Tolls", 9.0), ("TOLL_REIMBURSEMENT", "More tolls", 2.19)),
    )
    assert parsed is not None and parsed.toll_cents == 1119


def test_dollars_become_cents_without_losing_one() -> None:
    """140.4 * 100 is 14039.999... as a float, and int() of that is a cent
    short. 0.29 and 1.15 are the same trap."""
    for amount, cents in ((140.4, 14040), (0.29, 29), (1.15, 115), (40.71, 4071)):
        parsed = parse_turo_invoice(RES, _body(("TOLL_REIMBURSEMENT", "Tolls", amount)))
        assert parsed is not None and parsed.toll_cents == cents, amount


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"error": "not found"},
        {"invoiceId": 1, "total": {"amount": 5}},
        {"invoiceId": 1, "lineItems": []},
        # One line unreadable makes the whole thing unreadable: keeping the
        # rest would read as "these are all its charges".
        {**_body(("TOLL_REIMBURSEMENT", "Tolls", 9.0)), "lineItems": [{"type": "TOLL"}]},
        {**_body(("TOLL_REIMBURSEMENT", "Tolls", 9.0)), "lineItems": ["x"]},
    ],
)
def test_anything_that_is_not_an_invoice_is_refused(body: dict) -> None:
    assert parse_turo_invoice(RES, body) is None


# ---------------------------------------------------------------------------
# Applying it
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
        vehicle_id=car.id, turo_trip_id=RES, guest_name="Austin",
        starts_at=ENDS - td(days=3), ends_at=ENDS,
        state=TripState.completed, source=TripSource.email,
    )
    session.add(trip)
    session.flush()
    return trip


def _crossing(session, car, rental, *, cents, imported_at):
    toll = Toll(
        vehicle_id=car.id, trip_id=rental.id, occurred_at=ENDS - td(hours=5),
        plaza="CRZ", amount_cents=cents, fingerprint=f"t-{uuid.uuid4()}",
        imported_at=imported_at,
    )
    session.add(toll)
    session.flush()
    return toll


def _emailed(session, rental, *, total, lines=(), state="charged", invoice_id=None, seen=None):
    """An invoice as the mail recorded it."""
    seen = seen or NOW - td(days=5)
    row = ReimbursementInvoice(
        fingerprint=f"inv:{invoice_id}" if invoice_id else f"res:{RES}:{total}",
        reservation_id=RES, turo_invoice_id=invoice_id, guest_name="Austin",
        state=state, total_cents=total, lines=[list(line) for line in lines],
        toll_cents=None, trip_id=rental.id, first_seen_at=seen, last_seen_at=seen,
        charged_at=seen if state == "charged" else None,
    )
    session.add(row)
    session.flush()
    return row


def _apply(session, body):
    parsed = parse_turo_invoice(RES, body)
    assert parsed is not None
    result = TuroInvoiceResult()
    row = apply_turo_invoice(session, parsed, now=NOW, result=result)
    return row, result


@requires_db
def test_an_unitemised_charge_learns_its_breakdown(session, car, rental) -> None:
    """Austin's case end to end: the $140.40 was $40.71 of tolls and the rest
    distance, it was charged, so the crossings are paid — not to be filed."""
    toll = _crossing(session, car, rental, cents=4071, imported_at=NOW - td(days=10))
    row = _emailed(session, rental, total=14040)

    applied, result = _apply(session, AUSTIN)

    assert applied.id == row.id, "matched on its total, not recorded twice"
    assert applied.toll_cents == 4071
    assert applied.lines == [["Tolls", 4071], ["Additional distance", 9969]]
    assert applied.turo_invoice_id == "113672232"
    assert toll.recovered_at is not None, "charged, and the toll line covers it"
    assert result.tolls_recovered == 1
    assert result.newly_itemised == [f"{RES}: $40.71 of $140.40 was tolls"]


@requires_db
def test_an_asked_but_unpaid_breakdown_marks_the_crossings_asked(
    session, car, rental
) -> None:
    toll = _crossing(session, car, rental, cents=4071, imported_at=NOW - td(days=10))
    _emailed(session, rental, total=14040, state="filed")
    _apply(session, AUSTIN)
    assert toll.filed_at == NOW
    assert toll.recovered_at is None, "asked, not paid"


@requires_db
def test_a_crossing_imported_after_the_ask_is_still_owed(session, car, rental) -> None:
    """The invoice was asked when the mail first saw it. A crossing imported
    since was not part of it, and stamping it would mean nobody ever asks."""
    seen = NOW - td(days=5)
    before = _crossing(session, car, rental, cents=4071, imported_at=seen - td(days=1))
    after = _crossing(session, car, rental, cents=900, imported_at=seen + td(days=1))
    _emailed(session, rental, total=14040, state="filed", seen=seen)
    _apply(session, AUSTIN)
    assert before.filed_at is not None
    assert after.filed_at is None


@requires_db
def test_no_toll_line_frees_the_crossings_to_be_filed(
    api_client, session, car, rental
) -> None:
    """The other answer Turo can give: none of it was tolls. Then the rental
    stops being held back and the button can ask for them."""
    _crossing(session, car, rental, cents=4071, imported_at=NOW - td(days=10))
    _emailed(session, rental, total=14040)
    session.commit()
    assert api_client.get("/api/invoices/next-draft").status_code == 404, "held back"

    _apply(session, _body(("ADDITIONAL_DISTANCE", "Additional distance", 140.40)))
    session.commit()
    out = api_client.get("/api/invoices/next-draft")
    assert out.status_code == 200
    assert out.json()["total_cents"] == 4071


@requires_db
def test_two_rows_of_one_invoice_become_one(session, car, rental) -> None:
    """The "filed" email linked the invoice and the "charged" one did not, so
    the mail made two rows for one invoice — Austin's $50.00 ticket was asked
    for twice in the ledger. Reading the invoice folds them together, keeping
    the id-less row because its fingerprint is the one the charged email keeps
    producing."""
    by_id = _emailed(session, rental, total=14040, state="filed", invoice_id="113672232")
    charged = _emailed(session, rental, total=14040)
    applied, result = _apply(session, AUSTIN)
    session.flush()
    assert applied.id == charged.id
    assert result.merged == 1
    assert session.get(ReimbursementInvoice, by_id.id) is None
    assert applied.state == "charged" and applied.turo_invoice_id == "113672232"


@requires_db
def test_it_matches_the_extensions_own_filing_by_reimbursement_id(
    session, car, rental
) -> None:
    """The extension records Turo's reimbursementId, the mail links the
    invoiceId, and whether those are one number has not been established."""
    # Itemised as `/filed` writes it, so matching on the total cannot find it
    # and the id is the only way there.
    ours = _emailed(
        session, rental, total=14040, state="filed", invoice_id="5550001",
        lines=[("Tolls", 14040)],
    )
    applied, _ = _apply(session, AUSTIN)
    assert applied.id == ours.id


@requires_db
def test_an_itemised_invoice_is_never_matched_on_amount(session, car, rental) -> None:
    """An itemised row has already said what it charged. Pinning this
    breakdown onto it because the totals agree would overwrite a different
    invoice's lines."""
    other = _emailed(session, rental, total=14040, lines=[("Cleaning", 14040)])
    applied, result = _apply(session, AUSTIN)
    assert applied.id != other.id
    assert other.lines == [["Cleaning", 14040]]
    assert result.created == 1


@requires_db
def test_two_unitemised_invoices_of_one_amount_are_not_guessed_between(
    session, car, rental
) -> None:
    _emailed(session, rental, total=14040)
    second = ReimbursementInvoice(
        fingerprint=f"res:{RES}:14040:b", reservation_id=RES, state="charged",
        total_cents=14040, lines=[], trip_id=rental.id, last_seen_at=NOW,
    )
    session.add(second)
    session.flush()
    _, result = _apply(session, AUSTIN)
    assert result.created == 1 and result.matched == 0


@requires_db
def test_an_invoice_the_mail_never_saw_is_recorded_as_asked(session, car, rental) -> None:
    """Whatever its status says. "Charged" is the claim that ticks crossings
    off as paid, and the status values have never been read unmasked."""
    toll = _crossing(session, car, rental, cents=4071, imported_at=NOW - td(days=10))
    applied, result = _apply(session, AUSTIN)
    assert result.created == 1
    assert applied.state == "filed"
    assert applied.fingerprint == "inv:113672232"
    assert toll.recovered_at is None
    assert result.statuses == [f"{RES}: REIMBURSEMENT_PAID_BY_GUEST_EXAMPLE"]


@requires_db
def test_reading_it_twice_changes_nothing_the_second_time(session, car, rental) -> None:
    _crossing(session, car, rental, cents=4071, imported_at=NOW - td(days=10))
    _emailed(session, rental, total=14040, state="filed")
    _, first = _apply(session, AUSTIN)
    _, second = _apply(session, AUSTIN)
    assert first.tolls_asked == 1
    assert second.tolls_asked == 0 and second.newly_itemised == []
    assert second.matched == 1 and second.created == 0


# ---------------------------------------------------------------------------
# Which invoices to fetch, and the endpoint
# ---------------------------------------------------------------------------


@requires_db
def test_only_invoices_that_did_not_answer_are_wanted(session, rental) -> None:
    _emailed(session, rental, total=100, state="filed", invoice_id="1")  # no lines
    _emailed(session, rental, total=200, lines=[("Tickets", 200)], invoice_id="2")
    _emailed(session, rental, total=300, lines=[("Tolls and fuel", 300)], invoice_id="3")
    _emailed(session, rental, total=400)  # no id to fetch by
    assert sorted(wanted_invoices(session)) == [(RES, "1"), (RES, "3")]


@requires_db
def test_the_pull_is_told_which_invoices_to_read(api_client, session, rental) -> None:
    _emailed(session, rental, total=100, state="filed", invoice_id="1")
    session.commit()
    out = api_client.get("/api/turo/wanted").json()
    assert out["invoices"] == [[RES, "1"]]
    assert out["invoice_path"] == "/api/v2/reservations/{id}/reimbursement/invoice/{invoice}"


@requires_db
def test_posting_bodies_reads_them_and_counts_what_it_could_not(
    api_client, monkeypatch, session, car, rental
) -> None:
    monkeypatch.delenv("TOLLS_TOKEN", raising=False)
    _crossing(session, car, rental, cents=4071, imported_at=NOW - td(days=10))
    _emailed(session, rental, total=14040)
    session.commit()
    out = api_client.post(
        "/api/turo/invoices",
        json={"invoices": [
            {"reservation_id": RES, "body": AUSTIN},
            {"reservation_id": RES, "body": {"error": "nope"}},
        ]},
    )
    assert out.status_code == 200, out.text
    body = out.json()
    assert body["seen"] == 1 and body["unparsed"] == 1 and body["matched"] == 1
    assert body["itemised"] == [f"{RES}: $40.71 of $140.40 was tolls"]
    row = api_client.get("/api/invoices/ledger").json()["rows"][0]
    assert row["state"] == "settled"


@requires_db
def test_posting_bodies_needs_the_token(api_client, monkeypatch) -> None:
    monkeypatch.setenv("TOLLS_TOKEN", "s3cret")
    out = api_client.post("/api/turo/invoices", json={"invoices": []})
    assert out.status_code == 401


# ---------------------------------------------------------------------------
# A toll line nothing here was stamped against
# ---------------------------------------------------------------------------


@requires_db
def test_turo_charging_tolls_none_of_ours_were_stamped_for_is_not_drafted(
    api_client, session, car, rental
) -> None:
    """Every crossing imported after the invoice was first seen, so none was
    stamped — yet Turo's toll line says tolls were charged. The ledger has
    always called this "check Turo's toll line"; the button used to file it
    anyway."""
    seen = NOW - td(days=5)
    _crossing(session, car, rental, cents=4071, imported_at=seen + td(days=1))
    _emailed(session, rental, total=14040, state="filed", seen=seen)
    _apply(session, AUSTIN)
    session.commit()
    row = api_client.get("/api/invoices/ledger").json()["rows"][0]
    assert row["state"] == "check Turo's toll line"
    assert api_client.get("/api/invoices/next-draft").status_code == 404


# ---------------------------------------------------------------------------
# Finding a rental's invoices without anyone opening them
# ---------------------------------------------------------------------------


def _hub(*ids: int, status: str = "PAID") -> dict:
    """`/api/reservations/<id>/invoice-hub`, shaped as observed on 59077848."""
    return {
        "cta": {"button": {"buttonType": "PRIMARY", "label": "Create invoice"}},
        "emptyState": None,
        "sections": [{
            "type": "RESOLVED",
            "title": "Resolved",
            "invoices": [
                {
                    "amount": {"amount": 5000, "currencyCode": "USD"},
                    "invoiceId": invoice_id,
                    "invoiceLabel": "Invoice #000000000",
                    "status": status,
                    "title": "Tickets",
                    "type": "INCIDENTAL",
                    "description": "Paid by guest on 07/25/2026",
                }
                for invoice_id in ids
            ],
        }],
    }


def test_a_hub_lists_its_invoice_ids() -> None:
    listed = parse_hub(_hub(113672232, 113672299))
    assert listed is not None
    assert [i.invoice_id for i in listed] == ["113672232", "113672299"]
    assert listed[0].status == "PAID"


def test_an_empty_hub_lists_nothing_and_a_non_hub_is_refused() -> None:
    assert parse_hub({"sections": [], "emptyState": {"title": "No invoices"}}) == []
    assert parse_hub({"error": "nope"}) is None


@requires_db
def test_a_hub_invoice_known_only_as_a_total_is_read(session, rental) -> None:
    """Austin's $140.40: the mail's row has no id, so the hub's id for it is
    one this app does not know — which is exactly what makes it worth reading."""
    _emailed(session, rental, total=14040)
    listed = parse_hub(_hub(113672232))
    assert listed is not None
    assert hub_to_read(session, RES, listed) == [(RES, "113672232")]


@requires_db
def test_a_hub_invoice_already_answered_is_not_read_again(session, rental) -> None:
    """Every pull reads every hub. Without this it would re-read every invoice
    on the account each time, which is the kind of traffic that gets noticed."""
    _emailed(session, rental, total=5000, lines=[("Tickets", 5000)], invoice_id="1")
    _emailed(session, rental, total=100, invoice_id="2")  # known, but no breakdown
    listed = parse_hub(_hub(1, 2, 3))
    assert listed is not None
    assert hub_to_read(session, RES, listed) == [(RES, "2"), (RES, "3")]


@requires_db
def test_reading_an_invoice_keeps_turos_invoice_id(session, car, rental) -> None:
    """The extension's own filing is recorded under the reimbursement id. Once
    its invoice is read, the row carries the invoice id the hub lists, so the
    next pull knows it."""
    ours = _emailed(
        session, rental, total=14040, state="filed", invoice_id="5550001",
        lines=[("Tolls", 14040)],
    )
    _apply(session, AUSTIN)
    assert ours.turo_invoice_id == "113672232"
    listed = parse_hub(_hub(113672232))
    assert listed is not None
    assert hub_to_read(session, RES, listed) == []


@requires_db
def test_the_mail_lands_on_the_row_turo_already_described(session, car, rental) -> None:
    """The double count this closes: a row keyed by the reimbursement id, and
    the email for the same invoice keyed by its invoice id, used to be two
    invoices for one ask."""
    from turonomics_api.gmail.parse import ParsedInvoice
    from turonomics_api.ingest.reimbursements import record_invoice

    # Filed by the extension, so keyed `inv:<reimbursement id>`; then read off
    # Turo, which gives it the invoice id the email links.
    ours = _emailed(
        session, rental, total=14040, state="filed", invoice_id="5550001",
        lines=[("Tolls", 14040)],
    )
    _apply(session, AUSTIN)
    assert ours.fingerprint == "inv:5550001"
    before = session.query(ReimbursementInvoice).count()
    record_invoice(
        session,
        ParsedInvoice(
            state="charged", reservation_id=RES, guest_name="Austin",
            total_cents=14040, turo_invoice_id="113672232",
        ),
        now=NOW,
    )
    assert session.query(ReimbursementInvoice).count() == before
    assert ours.state == "charged", "the email moved the same row forward"


@requires_db
def test_the_whole_pull_finds_austins_breakdown_unaided(
    api_client, monkeypatch, session, car, rental
) -> None:
    """Details, then hubs, then invoices — the order the extension calls them,
    with nobody opening a page. With the real figures: Turo's page says
    $156.00, the email said $140.40."""
    monkeypatch.delenv("TOLLS_TOKEN", raising=False)
    _crossing(session, car, rental, cents=4071, imported_at=NOW - td(days=10))
    _emailed(session, rental, total=5000, lines=[("Tickets", 5000)], invoice_id="7")
    _emailed(session, rental, total=14040)
    session.commit()

    detail = {
        "id": int(RES),
        "booking": {},
        "reservationActions": ["VIEW_INVOICE_HUB"],
    }
    details = api_client.post("/api/turo/details", json={"details": [detail]}).json()
    assert details["invoice_hubs"] == [RES]

    hubs = api_client.post(
        "/api/turo/hubs", json={"hubs": [{"reservation_id": RES, "body": _hub(7, 113672232)}]}
    ).json()
    assert hubs["listed"] == 2
    # All of them, while the $140.40 is unmatched: which one it is, only
    # reading them can say.
    assert hubs["to_read"] == [[RES, "7"], [RES, "113672232"]]

    distance = _body(("ADDITIONAL_DISTANCE", "Additional distance", 156.00))
    out = api_client.post(
        "/api/turo/invoices",
        json={"invoices": [{"reservation_id": RES, "body": distance}]},
    ).json()
    assert out["created"] == 0, "the $156.00 is the email's $140.40, not a new invoice"
    assert out["itemised"] == [f"{RES}: none of $140.40 was tolls"]
    row = api_client.get("/api/invoices/ledger").json()["rows"][0]
    assert row["state"] == "to bill", "freed: it was distance"
    assert row["asked_cents"] == 5000 + 14040, "each invoice once"
    assert api_client.get("/api/invoices/next-draft").json()["total_cents"] == 4071

    again = api_client.post(
        "/api/turo/hubs", json={"hubs": [{"reservation_id": RES, "body": _hub(7, 113672232)}]}
    ).json()
    assert again["to_read"] == [], "matched now, so nothing is re-read"

    # And if it is read again anyway, it is not news a second time.
    repeat = api_client.post(
        "/api/turo/invoices",
        json={"invoices": [{"reservation_id": RES, "body": distance}]},
    ).json()
    assert repeat["itemised"] == []


@requires_db
def test_folding_two_rows_keeps_the_most_advanced_state(session, car, rental) -> None:
    """The kept row may be the one that only saw "filed". If the other saw
    "charged", the money arrived, and folding must not un-charge it."""
    paid = NOW - td(days=2)
    by_id = _emailed(session, rental, total=14040, state="charged", invoice_id="113672232")
    by_id.charged_at = paid
    _emailed(session, rental, total=14040, state="filed")
    applied, _ = _apply(session, AUSTIN)
    assert applied.state == "charged"
    assert applied.charged_at == paid


@requires_db
def test_folding_two_rows_keeps_the_earlier_ask(session, car, rental) -> None:
    """Which crossings an invoice can have covered is decided by when it was
    first seen. The kept row may have been seen later than its duplicate; a
    crossing imported between the two sightings arrived after the ask, so it
    was not part of it and is still owed. Taking the later date would stamp it
    and nobody would ever ask for it."""
    early, late = NOW - td(days=9), NOW - td(days=2)
    between = _crossing(session, car, rental, cents=900, imported_at=NOW - td(days=5))
    before = _crossing(session, car, rental, cents=4071, imported_at=NOW - td(days=12))
    _emailed(session, rental, total=14040, state="filed", invoice_id="113672232", seen=early)
    _emailed(session, rental, total=14040, state="filed", seen=late)
    applied, _ = _apply(session, AUSTIN)
    assert applied.first_seen_at == early
    assert before.filed_at is not None
    assert between.filed_at is None


@requires_db
def test_the_email_reports_the_hosts_share_of_a_distance_charge(session, car, rental) -> None:
    """Both live examples: $156.00 -> $140.40 and $45.88 -> $41.29. Tolls
    carry no cut, so a mixed invoice nets only the rest."""
    for gross, net in ((156.00, 14040), (45.88, 4129)):
        parsed = parse_turo_invoice(RES, _body(("ADDITIONAL_DISTANCE", "Distance", gross)))
        assert parsed is not None
        assert net in _amounts(parsed), gross
    mixed = parse_turo_invoice(RES, AUSTIN)
    assert mixed is not None
    assert 4071 + 8972 in _amounts(mixed), "99.69 * 0.9 = 89.72, tolls untouched"


@requires_db
def test_a_net_match_on_an_itemised_row_needs_the_same_charges(session, car, rental) -> None:
    """Gross line amounts are what the email itemises, so they must agree. A
    cleaning invoice that happens to net to the same total is another invoice."""
    tickets = _emailed(session, rental, total=4500, lines=[("Tickets", 5000)])
    cleaning = _emailed(session, rental, total=14040, lines=[("Cleaning", 9000), ("Smoking", 6600)])
    cleaning.fingerprint = f"res:{RES}:cleaning"
    applied, _ = _apply(session, _body(("TICKET_REIMBURSEMENT", "Tickets", 50.00)))
    assert applied.id == tickets.id
    other, result = _apply(
        session, _body(("ADDITIONAL_DISTANCE", "Distance", 156.00), invoiceId=2, reimbursementId=3)
    )
    assert other.id != cleaning.id and result.created == 1


@requires_db
def test_two_invoices_of_one_size_are_not_guessed_between_by_net_either(
    session, car, rental
) -> None:
    _emailed(session, rental, total=14040)
    _emailed(session, rental, total=15600, state="filed", invoice_id="999")
    _, result = _apply(session, _body(("ADDITIONAL_DISTANCE", "Distance", 156.00)))
    assert result.created == 1 and result.matched == 0


@requires_db
def test_reading_hubs_needs_the_token(api_client, monkeypatch) -> None:
    monkeypatch.setenv("TOLLS_TOKEN", "s3cret")
    assert api_client.post("/api/turo/hubs", json={"hubs": []}).status_code == 401
