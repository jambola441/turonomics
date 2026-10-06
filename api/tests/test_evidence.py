"""Tests for the evidence sheet and the invoice draft.

Turo attaches evidence images per line item, so a toll invoice filed without
one is a number with nothing behind it. Two things are worth testing hard:
that the sheet renders at all (an unescaped ampersand makes it a broken image,
and real plaza names contain one), and that it says where its figures came
from — it is this app's ledger, not a copy of an E-ZPass page.
"""

from __future__ import annotations

import uuid
import xml.dom.minidom
from datetime import UTC, datetime
from datetime import timedelta as td

import pytest

from turonomics_api.db.models import (
    ReimbursementInvoice,
    Toll,
    Trip,
    TripSource,
    TripState,
    Vehicle,
)
from turonomics_api.ingest.evidence import EvidenceRow, EvidenceSheet, evidence_svg

from .conftest import requires_db

NOW = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
ENDS = NOW - td(days=10)


def _sheet(**overrides: object) -> EvidenceSheet:
    base: dict[str, object] = {
        "reservation_id": "58358939",
        "guest_name": "Dylan",
        "vehicle": "Jolene",
        "plate": "LWH4685",
        "starts_at": ENDS - td(days=2),
        "ends_at": ENDS,
        "rows": [
            EvidenceRow(occurred_at=ENDS - td(hours=20), plaza="MTAB&T RKB", amount_cents=679),
            EvidenceRow(
                occurred_at=ENDS - td(hours=3), plaza="Verrazzano-Narrows", amount_cents=1100
            ),
        ],
        "imported_at": NOW,
    }
    base.update(overrides)
    return EvidenceSheet(**base)  # type: ignore[arg-type]


def test_a_plaza_with_an_ampersand_does_not_break_the_image() -> None:
    """"MTAB&T RKB" is the Robert Kennedy bridge, and it is in every statement
    this fleet imports. Unescaped, it makes XML a browser refuses to render —
    so the evidence becomes a broken image rather than an error anyone sees."""
    svg = evidence_svg(_sheet())
    xml.dom.minidom.parseString(svg)  # raises if malformed
    assert "MTAB&amp;T" in svg
    assert "MTAB&T" not in svg


def test_a_guest_name_with_an_ampersand_is_escaped_too() -> None:
    svg = evidence_svg(_sheet(guest_name="Ben & Jo"))
    xml.dom.minidom.parseString(svg)
    assert "Ben &amp; Jo" in svg


def test_the_total_is_the_sum_of_the_crossings() -> None:
    svg = evidence_svg(_sheet())
    assert "$17.79" in svg
    assert "2 crossing(s)" in svg


def test_each_crossing_appears_with_its_plaza_and_amount() -> None:
    svg = evidence_svg(_sheet())
    assert "$6.79" in svg and "$11.00" in svg
    assert "Verrazzano-Narrows" in svg


def test_the_sheet_says_whose_record_it_is() -> None:
    """The line that keeps this honest evidence rather than a lookalike. A
    generated image dressed as somebody else's document misrepresents where the
    figures came from."""
    svg = evidence_svg(_sheet())
    assert "Prepared by Turonomics" in svg
    assert "available on request" in svg


def test_the_import_date_is_named_when_it_is_known() -> None:
    assert "imported 5 Oct 2026" in evidence_svg(_sheet())
    assert "imported" not in evidence_svg(_sheet(imported_at=None))


def test_times_are_shown_in_the_fleets_own_zone() -> None:
    """A crossing at 02:05 UTC is 22:05 the evening before in Brooklyn, and a
    guest reading "2am" about a trip that ended at ten would reasonably
    dispute it."""
    sheet = _sheet(
        rows=[
            EvidenceRow(
                occurred_at=datetime(2026, 7, 5, 2, 5, tzinfo=UTC),
                plaza="RKB",
                amount_cents=679,
            )
        ]
    )
    svg = evidence_svg(sheet)
    assert "10:05 PM" in svg
    assert "4 Jul 2026" in svg, "and the evening before, not the 5th"


def test_a_sheet_with_one_crossing_is_not_pluralised_oddly() -> None:
    svg = evidence_svg(_sheet(rows=[EvidenceRow(ENDS, "RKB", 500)]))
    assert "1 crossing(s)" in svg


def test_an_unnamed_guest_does_not_leave_a_hole() -> None:
    svg = evidence_svg(_sheet(guest_name=None))
    assert "the guest" in svg
    assert "None" not in svg


def test_a_car_with_no_plate_on_file_omits_it_rather_than_saying_none() -> None:
    svg = evidence_svg(_sheet(plate=None))
    assert "None" not in svg
    assert "Jolene" in svg


# ---------------------------------------------------------------------------
# The draft
# ---------------------------------------------------------------------------


@pytest.fixture()
def car(session):
    vehicle = Vehicle(
        nickname="Jolene", make="Toyota", model="Corolla", year=2024, plate="LWH4685"
    )
    session.add(vehicle)
    session.flush()
    return vehicle


@pytest.fixture()
def rental(session, car):
    trip = Trip(
        vehicle_id=car.id,
        turo_trip_id="58358939",
        guest_name="Dylan",
        starts_at=ENDS - td(days=2),
        ends_at=ENDS,
        state=TripState.completed,
        source=TripSource.email,
    )
    session.add(trip)
    session.flush()
    return trip


def _crossing(session, car, rental, *, at, cents=679, plaza="MTAB&T RKB"):
    toll = Toll(
        vehicle_id=car.id,
        trip_id=rental.id,
        occurred_at=at,
        plaza=plaza,
        amount_cents=cents,
        fingerprint=f"evidence-{uuid.uuid4()}",
    )
    session.add(toll)
    session.flush()
    return toll


@requires_db
def test_the_draft_carries_the_crossings_and_their_evidence(
    api_client, session, car, rental
) -> None:
    _crossing(session, car, rental, at=ENDS - td(hours=20))
    _crossing(session, car, rental, at=ENDS - td(hours=3), cents=1100, plaza="VNB")
    session.commit()

    out = api_client.get(f"/api/invoices/{rental.id}/draft").json()
    assert out["turo_trip_id"] == "58358939"
    assert out["total_cents"] == 1779
    assert len(out["lines"]) == 2
    assert "Prepared by Turonomics" in out["evidence_svg"]
    xml.dom.minidom.parseString(out["evidence_svg"])


@requires_db
def test_a_rental_with_nothing_outstanding_has_no_draft(
    api_client, session, car, rental
) -> None:
    """Filing nothing is not a thing to do, and an invoice for zero is worse
    than a 404."""
    session.commit()
    assert api_client.get(f"/api/invoices/{rental.id}/draft").status_code == 404


@requires_db
def test_turos_flag_is_reported_and_not_acted_on(
    api_client, session, car, rental
) -> None:
    """`allowedToRequestReimbursement` was briefly a hard gate here.

    Against the live account it is false for all 37 rentals, including the one
    that was then filed by hand and charged to the guest. Whatever it means, it
    is not "a reimbursement may still be requested" — and a gate built on it
    blocked every invoice this app could otherwise raise.
    """
    _crossing(session, car, rental, at=ENDS - td(hours=3))
    rental.can_file_reimbursement = False
    session.commit()

    out = api_client.get(f"/api/invoices/{rental.id}/draft").json()
    assert out["can_file"] is True, "the window decides, and it is open"
    assert out["turo_allows_request"] is False, "reported, beside it"
    assert out["days_left"] is not None and out["days_left"] > 0


@requires_db
def test_the_window_decides_whether_a_draft_can_be_filed(
    api_client, session, car, rental
) -> None:
    _crossing(session, car, rental, at=ENDS - td(hours=3))
    session.commit()
    out = api_client.get(f"/api/invoices/{rental.id}/draft").json()
    assert out["can_file"] is True
    assert out["turo_allows_request"] is None, "never asked"


@requires_db
def test_a_rental_past_the_window_cannot_be_filed(
    api_client, session, car, rental
) -> None:
    rental.starts_at = NOW - td(days=200)
    rental.ends_at = NOW - td(days=198)
    _crossing(session, car, rental, at=rental.ends_at - td(hours=2))
    session.commit()
    out = api_client.get(f"/api/invoices/{rental.id}/draft").json()
    assert out["can_file"] is False
    assert out["days_left"] is not None and out["days_left"] < 0


@requires_db
def test_an_unknown_rental_is_a_404_not_an_empty_draft(api_client) -> None:
    assert api_client.get(f"/api/invoices/{uuid.uuid4()}/draft").status_code == 404


def test_both_ends_of_the_trip_window_are_written_the_same_way() -> None:
    """It read "9 Jul 2026, 7:00 AM — Sun 12 Jul 2026, 2:00 PM" at first: a
    weekday on one end and not the other, on a document a guest reads."""
    svg = evidence_svg(_sheet())
    window = svg.split("Trip: ")[1].split("</text>")[0]
    before, after = window.split(" — ")
    assert before.split()[0].rstrip(",").isalpha(), window
    assert after.split()[0].rstrip(",").isalpha(), window


# ---------------------------------------------------------------------------
# Choosing what to file, and recording that it was
# ---------------------------------------------------------------------------


@requires_db
def test_the_soonest_deadline_is_drafted_first(api_client, session, car) -> None:
    """Which rental to file for is a judgement about money and deadlines, so it
    lives here rather than in a browser plugin that has to be side-loaded to
    change."""
    soon = Trip(
        vehicle_id=car.id, turo_trip_id="111", guest_name="Soon",
        starts_at=NOW - td(days=87), ends_at=NOW - td(days=86),
        state=TripState.completed, source=TripSource.email,
    )
    later = Trip(
        vehicle_id=car.id, turo_trip_id="222", guest_name="Later",
        starts_at=NOW - td(days=10), ends_at=NOW - td(days=9),
        state=TripState.completed, source=TripSource.email,
    )
    session.add_all([soon, later])
    session.flush()
    _crossing(session, car, soon, at=soon.ends_at - td(hours=2), cents=500)
    # Bigger, but not nearly as close to expiring.
    _crossing(session, car, later, at=later.ends_at - td(hours=2), cents=9000)
    session.commit()

    out = api_client.get("/api/invoices/next-draft").json()
    assert out["turo_trip_id"] == "111"
    assert out["amount_dollars"] == 5.0


@requires_db
def test_a_rental_already_asked_about_is_not_drafted_again(
    api_client, session, car, rental
) -> None:
    """Asking twice for the same crossings is a dispute with a guest, which is
    what this whole feature exists to avoid."""
    _crossing(session, car, rental, at=ENDS - td(hours=2))
    session.commit()
    assert api_client.get("/api/invoices/next-draft").status_code == 200

    api_client.post(
        f"/api/invoices/{rental.id}/filed",
        json={"reimbursement_id": 9001, "amount_cents": 679},
    )
    assert api_client.get("/api/invoices/next-draft").status_code == 404


@requires_db
def test_a_rental_turo_flags_is_still_offered(api_client, session, car, rental) -> None:
    """The regression this file exists to prevent a second time: with that flag
    as a gate, `next-draft` answered "nothing to file" for every rental on the
    account while $1,218 sat uncollected."""
    _crossing(session, car, rental, at=ENDS - td(hours=2))
    rental.can_file_reimbursement = False
    session.commit()
    out = api_client.get("/api/invoices/next-draft")
    assert out.status_code == 200
    assert out.json()["turo_allows_request"] is False


@requires_db
def test_filing_is_recorded_as_asked_not_as_paid(
    api_client, session, car, rental
) -> None:
    """The guest has been asked and has not paid. Treating the ask as the
    payment is how a crossing silently stops being chased."""
    _crossing(session, car, rental, at=ENDS - td(hours=2))
    session.commit()
    out = api_client.post(
        f"/api/invoices/{rental.id}/filed",
        json={"reimbursement_id": 9001, "amount_cents": 679},
    ).json()
    assert out["recorded"] is True
    assert out["fingerprint"] == "inv:9001"

    row = api_client.get("/api/invoices").json()["invoices"][0]
    assert row["pending_cents"] == 679, "asked for"
    assert row["charged_cents"] == 0, "and not collected"


@requires_db
def test_recording_the_same_filing_twice_is_harmless(
    api_client, session, car, rental
) -> None:
    """A retried post, or the email arriving before this does. The fingerprint
    is the mail parser's own scheme so the two land on one row."""
    _crossing(session, car, rental, at=ENDS - td(hours=2))
    session.commit()
    body = {"reimbursement_id": 9001, "amount_cents": 679}
    assert api_client.post(f"/api/invoices/{rental.id}/filed", json=body).json()["recorded"]
    second = api_client.post(f"/api/invoices/{rental.id}/filed", json=body).json()
    assert second["recorded"] is False
    assert len(api_client.get("/api/invoices").json()["invoices"]) <= 1


@requires_db
def test_the_note_names_the_crossings_and_the_total(
    api_client, session, car, rental
) -> None:
    _crossing(session, car, rental, at=ENDS - td(hours=5))
    _crossing(session, car, rental, at=ENDS - td(hours=2), cents=1100)
    session.commit()
    message = api_client.get(f"/api/invoices/{rental.id}/draft").json()["message"]
    assert "2 tolls" in message
    assert "$17.79" in message
    # Nothing that reads as an accusation: they rented the car and drove it.
    for word in ("fail", "owe", "must", "unpaid", "liable"):
        assert word not in message.lower(), message


@requires_db
def test_a_single_crossing_is_not_pluralised(api_client, session, car, rental) -> None:
    _crossing(session, car, rental, at=ENDS - td(hours=2))
    session.commit()
    message = api_client.get(f"/api/invoices/{rental.id}/draft").json()["message"]
    assert "1 toll on your trip" in message


@requires_db
def test_filing_an_unknown_rental_is_refused(api_client) -> None:
    out = api_client.post(
        f"/api/invoices/{uuid.uuid4()}/filed",
        json={"reimbursement_id": 1, "amount_cents": 100},
    )
    assert out.status_code == 404


@requires_db
def test_recording_a_filing_is_gated_by_the_token(
    api_client, monkeypatch, session, car, rental
) -> None:
    _crossing(session, car, rental, at=ENDS - td(hours=2))
    session.commit()
    monkeypatch.setenv("TOLLS_TOKEN", "letmein")
    body = {"reimbursement_id": 9001, "amount_cents": 679}
    assert api_client.post(f"/api/invoices/{rental.id}/filed", json=body).status_code == 401
    assert (
        api_client.post(
            f"/api/invoices/{rental.id}/filed",
            json=body,
            headers={"Authorization": "Bearer letmein"},
        ).status_code
        == 200
    )


@requires_db
def test_an_expired_rental_is_never_the_next_to_file(api_client, session, car) -> None:
    """Past the window Turo will not take it, so offering it as the thing to do
    next wastes the one action a person was going to take."""
    expired = Trip(
        vehicle_id=car.id, turo_trip_id="333", guest_name="Gone",
        starts_at=NOW - td(days=200), ends_at=NOW - td(days=199),
        state=TripState.completed, source=TripSource.email,
    )
    session.add(expired)
    session.flush()
    # Large, and the soonest "deadline" of all by virtue of being past it.
    _crossing(session, car, expired, at=expired.ends_at - td(hours=2), cents=9000)
    session.commit()
    assert api_client.get("/api/invoices/next-draft").status_code == 404

    live = Trip(
        vehicle_id=car.id, turo_trip_id="444", guest_name="Live",
        starts_at=NOW - td(days=10), ends_at=NOW - td(days=9),
        state=TripState.completed, source=TripSource.email,
    )
    session.add(live)
    session.flush()
    _crossing(session, car, live, at=live.ends_at - td(hours=2), cents=500)
    session.commit()
    out = api_client.get("/api/invoices/next-draft").json()
    assert out["turo_trip_id"] == "444", "the live one, not the bigger expired one"


# ---------------------------------------------------------------------------
# A rental that was only partly filed
# ---------------------------------------------------------------------------
#
# Filing was recorded against the rental at first. That was safe against asking
# twice and silently wrong the other way: E-ZPass statements arrive weeks late
# and in batches, so a crossing can land on a rental that has already been
# invoiced — and with a reimbursement against its trip, it was skipped forever.


@requires_db
def test_a_crossing_that_arrives_after_filing_can_still_be_asked_for(
    api_client, session, car, rental
) -> None:
    first = _crossing(session, car, rental, at=ENDS - td(hours=5))
    session.commit()
    api_client.post(
        f"/api/invoices/{rental.id}/filed",
        json={"reimbursement_id": 9001, "amount_cents": first.amount_cents},
    )
    # Nothing left to ask for, so nothing is offered.
    assert api_client.get("/api/invoices/next-draft").status_code == 404

    # A later statement brings another crossing on the same rental.
    _crossing(session, car, rental, at=ENDS - td(hours=2), cents=1100, plaza="VNB")
    session.commit()

    out = api_client.get("/api/invoices/next-draft").json()
    assert out["trip_id"] == str(rental.id)
    assert out["total_cents"] == 1100, "the new one only"
    assert len(out["lines"]) == 1
    assert out["lines"][0]["plaza"] == "VNB"


@requires_db
def test_an_already_filed_crossing_is_not_in_the_draft(
    api_client, session, car, rental
) -> None:
    """The direct per-rental draft had no guard at all: called on its own it
    drafted every unrecovered crossing, including ones already asked for."""
    _crossing(session, car, rental, at=ENDS - td(hours=5))
    session.commit()
    api_client.post(
        f"/api/invoices/{rental.id}/filed",
        json={"reimbursement_id": 9001, "amount_cents": 679},
    )
    assert api_client.get(f"/api/invoices/{rental.id}/draft").status_code == 404

    _crossing(session, car, rental, at=ENDS - td(hours=2), cents=1100)
    session.commit()
    out = api_client.get(f"/api/invoices/{rental.id}/draft").json()
    assert out["total_cents"] == 1100
    assert out["amount_dollars"] == 11.0


@requires_db
def test_the_evidence_and_note_cover_only_what_is_being_asked_for(
    api_client, session, car, rental
) -> None:
    """A sheet listing crossings the guest already paid for, attached to an
    invoice that does not include them, is how a reasonable guest decides the
    whole thing is wrong."""
    _crossing(session, car, rental, at=ENDS - td(hours=5), plaza="ALREADYFILED")
    session.commit()
    api_client.post(
        f"/api/invoices/{rental.id}/filed",
        json={"reimbursement_id": 9001, "amount_cents": 679},
    )
    _crossing(session, car, rental, at=ENDS - td(hours=2), cents=1100, plaza="NEWONE")
    session.commit()

    out = api_client.get(f"/api/invoices/{rental.id}/draft").json()
    assert "NEWONE" in out["evidence_svg"]
    assert "ALREADYFILED" not in out["evidence_svg"]
    assert "1 toll on your trip" in out["message"]
    assert "$11.00" in out["message"]


@requires_db
def test_the_page_still_shows_what_was_asked_for(api_client, session, car, rental) -> None:
    """Filed is not paid. The crossings stay on the page as outstanding — they
    are simply not offered for filing again."""
    _crossing(session, car, rental, at=ENDS - td(hours=5))
    session.commit()
    api_client.post(
        f"/api/invoices/{rental.id}/filed",
        json={"reimbursement_id": 9001, "amount_cents": 679},
    )
    body = api_client.get("/api/invoices").json()
    assert body["billable_cents"] == 679, "still owed"
    assert body["invoices"][0]["pending_cents"] == 679, "and already asked for"


@requires_db
def test_a_fully_filed_rental_does_not_shadow_a_fileable_one(
    api_client, session, car
) -> None:
    """Without the skip, the fully-filed rental still sorts first on deadline
    and then drafts to a 404 — so the endpoint reports "nothing to file" while
    a live invoice sits behind it. Every other test passes either way, because
    none has a second rental waiting."""
    done = Trip(
        vehicle_id=car.id, turo_trip_id="555", guest_name="Done",
        starts_at=NOW - td(days=80), ends_at=NOW - td(days=79),
        state=TripState.completed, source=TripSource.email,
    )
    waiting = Trip(
        vehicle_id=car.id, turo_trip_id="666", guest_name="Waiting",
        starts_at=NOW - td(days=10), ends_at=NOW - td(days=9),
        state=TripState.completed, source=TripSource.email,
    )
    session.add_all([done, waiting])
    session.flush()
    _crossing(session, car, done, at=done.ends_at - td(hours=2), cents=500)
    _crossing(session, car, waiting, at=waiting.ends_at - td(hours=2), cents=1100)
    session.commit()

    api_client.post(
        f"/api/invoices/{done.id}/filed",
        json={"reimbursement_id": 9002, "amount_cents": 500},
    )
    out = api_client.get("/api/invoices/next-draft")
    assert out.status_code == 200, "the live one, not a 404 from the filed one"
    assert out.json()["turo_trip_id"] == "666"


@requires_db
def test_filing_again_does_not_restamp_the_earlier_crossings(
    api_client, session, car, rental
) -> None:
    """When each crossing was asked for is the record of what was asked and
    when. Re-stamping on a second filing overwrites the first invoice's date
    with the second's, which is exactly the thing to reach for in a dispute."""
    first = _crossing(session, car, rental, at=ENDS - td(hours=5))
    session.commit()
    api_client.post(
        f"/api/invoices/{rental.id}/filed",
        json={"reimbursement_id": 9001, "amount_cents": 679},
    )
    session.refresh(first)
    originally = first.filed_at
    assert originally is not None

    _crossing(session, car, rental, at=ENDS - td(hours=2), cents=1100)
    session.commit()
    api_client.post(
        f"/api/invoices/{rental.id}/filed",
        json={"reimbursement_id": 9002, "amount_cents": 1100},
    )
    session.refresh(first)
    assert first.filed_at == originally, "the first ask keeps its own date"


# ---------------------------------------------------------------------------
# The ledger: both sides, per rental
# ---------------------------------------------------------------------------


@requires_db
def test_the_ledger_splits_a_rental_into_asked_and_not(
    api_client, session, car, rental
) -> None:
    _crossing(session, car, rental, at=ENDS - td(hours=5))
    session.commit()
    api_client.post(
        f"/api/invoices/{rental.id}/filed",
        json={"reimbursement_id": 9001, "amount_cents": 679},
    )
    _crossing(session, car, rental, at=ENDS - td(hours=2), cents=1100)
    session.commit()

    out = api_client.get("/api/invoices/ledger").json()
    row = out["rows"][0]
    assert row["tolls_cents"] == 1779
    assert row["filed_cents"] == 679, "asked for"
    assert row["unfiled_cents"] == 1100, "arrived afterwards"
    assert row["recovered_cents"] == 0
    assert row["state"] == "partly billed"
    assert "has not been asked for" in row["note"]


@requires_db
def test_the_three_parts_always_sum_to_the_whole(
    api_client, session, car, rental
) -> None:
    """The property that makes the view worth looking at: every crossing is in
    exactly one of the three columns, so a reader can trust the row."""
    _crossing(session, car, rental, at=ENDS - td(hours=5))
    _crossing(session, car, rental, at=ENDS - td(hours=4), cents=1100)
    _crossing(session, car, rental, at=ENDS - td(hours=3), cents=250)
    session.commit()
    api_client.post(
        f"/api/invoices/{rental.id}/filed",
        json={"reimbursement_id": 9001, "amount_cents": 2029},
    )
    out = api_client.get("/api/invoices/ledger").json()
    for row in out["rows"]:
        assert row["unfiled_cents"] + row["filed_cents"] + row["recovered_cents"] == (
            row["tolls_cents"]
        )
    assert out["unfiled_cents"] + out["filed_cents"] + out["recovered_cents"] == (
        out["tolls_cents"]
    )


@requires_db
def test_a_settled_rental_is_in_the_ledger_but_not_the_invoice_list(
    api_client, monkeypatch, session, car, rental
) -> None:
    """The invoice list answers "what next" and leaves out what is done. The
    ledger answers "where did it go", which needs the done ones or the totals
    do not add up."""
    monkeypatch.delenv("TOLLS_TOKEN", raising=False)
    toll = _crossing(session, car, rental, at=ENDS - td(hours=2))
    toll.recovered_at = NOW
    session.commit()

    assert api_client.get("/api/invoices").json()["invoices"] == []
    out = api_client.get("/api/invoices/ledger").json()
    assert len(out["rows"]) == 1
    assert out["rows"][0]["state"] == "settled"
    assert out["rows"][0]["recovered_cents"] == 679


@requires_db
def test_turos_side_is_shown_beside_ours_not_merged_into_it(
    api_client, session, car, rental
) -> None:
    """Turo's totals bundle refuelling and tickets, so only its toll line is
    comparable. Both are reported; neither is corrected by the other."""
    _crossing(session, car, rental, at=ENDS - td(hours=2))
    session.add(
        ReimbursementInvoice(
            fingerprint="inv:777", reservation_id=rental.turo_trip_id,
            guest_name="Dylan", state="charged", total_cents=5000,
            lines=[["Tolls", 2500], ["Refueling", 2500]], toll_cents=2500,
            trip_id=rental.id, last_seen_at=NOW, charged_at=NOW,
        )
    )
    session.commit()
    row = api_client.get("/api/invoices/ledger").json()["rows"][0]
    assert row["tolls_cents"] == 679, "ours"
    assert row["charged_cents"] == 5000, "the whole invoice Turo charged"
    assert row["turo_toll_line_cents"] == 2500, "the part of it that was tolls"


@requires_db
def test_an_expired_rental_says_so_rather_than_reading_as_to_do(
    api_client, session, car, rental
) -> None:
    rental.starts_at = NOW - td(days=200)
    rental.ends_at = NOW - td(days=199)
    _crossing(session, car, rental, at=rental.ends_at - td(hours=2))
    session.commit()
    row = api_client.get("/api/invoices/ledger").json()["rows"][0]
    assert row["state"] == "expired"
    assert "cannot be filed" in row["note"]


@requires_db
def test_a_fully_asked_rental_reads_as_awaiting_payment(
    api_client, session, car, rental
) -> None:
    _crossing(session, car, rental, at=ENDS - td(hours=2))
    session.commit()
    api_client.post(
        f"/api/invoices/{rental.id}/filed",
        json={"reimbursement_id": 9001, "amount_cents": 679},
    )
    row = api_client.get("/api/invoices/ledger").json()["rows"][0]
    assert row["state"] == "awaiting payment"
    assert row["unfiled_cents"] == 0


@requires_db
def test_turo_charging_for_something_else_is_not_these_tolls_being_billed(
    api_client, session, car, rental
) -> None:
    """Found on real data: six rentals read as "partly billed" with nothing
    ever filed, because Turo had charged them for refuelling or a ticket. Its
    invoice totals say nothing about whose tolls are outstanding — the note
    even claimed the full amount "arrived after the first invoice" when there
    had been no first invoice.
    """
    _crossing(session, car, rental, at=ENDS - td(hours=2), cents=4071)
    session.add(
        ReimbursementInvoice(
            fingerprint="inv:888", reservation_id=rental.turo_trip_id,
            guest_name="Dylan", state="charged", total_cents=19040,
            lines=[["Tickets", 19040]], toll_cents=None,
            trip_id=rental.id, last_seen_at=NOW, charged_at=NOW,
        )
    )
    session.commit()

    row = api_client.get("/api/invoices/ledger").json()["rows"][0]
    assert row["state"] == "to bill", "none of these tolls has been asked for"
    assert row["note"] is None
    assert row["unfiled_cents"] == 4071
    # Turo's charge is still shown, because it is true and worth seeing.
    assert row["charged_cents"] == 19040
    assert row["turo_toll_line_cents"] is None


@requires_db
def test_a_turo_toll_line_that_did_not_reconcile_is_flagged(
    api_client, session, car, rental
) -> None:
    """Somebody has billed for tolls on this rental and it was not this app.
    Worth a person's eye before asking the guest again."""
    _crossing(session, car, rental, at=ENDS - td(hours=2), cents=7051)
    session.add(
        ReimbursementInvoice(
            fingerprint="inv:999", reservation_id=rental.turo_trip_id,
            guest_name="Dylan", state="charged", total_cents=3631,
            lines=[["Tolls", 3631]], toll_cents=3631,
            trip_id=rental.id, last_seen_at=NOW, charged_at=NOW,
        )
    )
    session.commit()

    row = api_client.get("/api/invoices/ledger").json()["rows"][0]
    assert row["state"] == "check Turo's toll line"
    assert "$36.31 of tolls" in row["note"]
    assert "$70.51 still outstanding" in row["note"]


@requires_db
def test_turos_flag_does_not_change_a_rows_state(
    api_client, session, car, rental
) -> None:
    """It is reported, so it can be looked at if it ever starts meaning
    something. It does not decide anything."""
    _crossing(session, car, rental, at=ENDS - td(hours=2), cents=2789)
    rental.can_file_reimbursement = False
    session.commit()

    row = api_client.get("/api/invoices/ledger").json()["rows"][0]
    assert row["state"] == "to bill", "there is money here and the window is open"
    assert row["turo_allows_request"] is False


# ---------------------------------------------------------------------------
# Money Turo has asked for without saying what for
# ---------------------------------------------------------------------------


def _turo_invoice(session, rental, *, total, lines, state="charged", toll=None, key="1"):
    row = ReimbursementInvoice(
        fingerprint=f"res:{rental.turo_trip_id}:{key}", reservation_id=rental.turo_trip_id,
        guest_name="Austin", state=state, total_cents=total, lines=lines, toll_cents=toll,
        trip_id=rental.id, last_seen_at=NOW, charged_at=NOW if state == "charged" else None,
    )
    session.add(row)
    return row


@requires_db
def test_a_rental_with_an_unitemised_charge_is_not_drafted(
    api_client, session, car, rental
) -> None:
    """59077848: $140.40 charged through an email that did not itemise, beside
    $40.71 of crossings. Nothing here can say the $140.40 was not those tolls,
    and drafting them asks the guest a second time if it was."""
    _crossing(session, car, rental, at=ENDS - td(hours=2), cents=4071)
    _turo_invoice(session, rental, total=14040, lines=[])
    session.commit()

    assert api_client.get("/api/invoices/next-draft").status_code == 404
    row = api_client.get("/api/invoices/ledger").json()["rows"][0]
    assert row["state"] == "check Turo's invoice"
    assert "$140.40" in row["note"] and "$40.71" in row["note"]


@requires_db
def test_a_charge_that_names_tolls_with_something_else_is_not_drafted(
    api_client, session, car, rental
) -> None:
    _crossing(session, car, rental, at=ENDS - td(hours=2))
    _turo_invoice(session, rental, total=5555, lines=[["Tolls and fuel", 5555]])
    session.commit()
    assert api_client.get("/api/invoices/next-draft").status_code == 404


@requires_db
def test_a_ticket_alone_does_not_hold_the_tolls_back(
    api_client, session, car, rental
) -> None:
    """The other direction, and the reason this is not "any invoice at all": an
    itemised ticket asked the guest for nothing to do with these crossings.
    Blocking on it would leave the tolls uncollected for the sake of a fine."""
    _crossing(session, car, rental, at=ENDS - td(hours=2))
    _turo_invoice(session, rental, total=5000, lines=[["Tickets", 5000]])
    session.commit()
    assert api_client.get("/api/invoices/next-draft").status_code == 200
    row = api_client.get("/api/invoices/ledger").json()["rows"][0]
    assert row["state"] == "to bill"


@requires_db
def test_one_unitemised_invoice_holds_back_a_rental_with_an_itemised_one(
    api_client, session, car, rental
) -> None:
    """Austin's shape exactly: the ticket itemised, the larger charge did not."""
    _crossing(session, car, rental, at=ENDS - td(hours=2), cents=4071)
    _turo_invoice(session, rental, total=5000, lines=[["Tickets", 5000]], key="a")
    _turo_invoice(session, rental, total=14040, lines=[], key="b")
    session.commit()
    assert api_client.get("/api/invoices/next-draft").status_code == 404
    row = api_client.get("/api/invoices/ledger").json()["rows"][0]
    assert "$140.40" in row["note"], "only the part nobody can read"


# ---------------------------------------------------------------------------
# What Turo charged, said plainly, and the whole account in the ledger
# ---------------------------------------------------------------------------


@requires_db
def test_a_charge_that_is_plainly_something_else_is_not_a_warning(
    api_client, session, car, rental
) -> None:
    """Austin's rental read "it did not itemise tolls — check before asking
    again" after Turo's page had said exactly what each charge was."""
    _crossing(session, car, rental, at=ENDS - td(hours=2), cents=4071)
    _turo_invoice(session, rental, total=5000, lines=[["Tickets", 5000]], key="a")
    _turo_invoice(session, rental, total=14040, lines=[["Additional distance", 15600]], key="b")
    session.commit()
    row = api_client.get("/api/invoices").json()["invoices"][0]
    assert row["charged_but_different"] is False
    assert row["charged_cents"] == 19040
    assert "Tickets $50.00" in row["charged_lines"]


@requires_db
def test_an_unreadable_charge_is_still_a_warning(api_client, session, car, rental) -> None:
    _crossing(session, car, rental, at=ENDS - td(hours=2), cents=4071)
    _turo_invoice(session, rental, total=14040, lines=[])
    session.commit()
    assert api_client.get("/api/invoices").json()["invoices"][0]["charged_but_different"] is True


@requires_db
def test_a_toll_line_that_disagrees_is_still_a_warning(api_client, session, car, rental) -> None:
    _crossing(session, car, rental, at=ENDS - td(hours=2), cents=4071)
    _crossing(session, car, rental, at=ENDS - td(hours=1), cents=900)
    session.add(
        ReimbursementInvoice(
            fingerprint="inv:tl", reservation_id=rental.turo_trip_id, state="charged",
            total_cents=1555, lines=[["Tolls", 1555]], toll_cents=1555, trip_id=rental.id,
            last_seen_at=NOW, charged_at=NOW,
        )
    )
    session.commit()
    row = api_client.get("/api/invoices").json()["invoices"][0]
    assert row["charged_but_different"] is True


@requires_db
def test_the_ledger_splits_turos_charges_by_kind(api_client, session, car, rental) -> None:
    _crossing(session, car, rental, at=ENDS - td(hours=2), cents=4071)
    _turo_invoice(session, rental, total=5000, lines=[["Tickets", 5000]], key="a")
    _turo_invoice(
        session, rental, total=20000,
        lines=[["Tolls", 1200], ["22 mi additional distance", 4000], ["Cleaning", 14800]],
        toll=1200, key="b",
    )
    _turo_invoice(session, rental, total=777, lines=[], key="c")
    _turo_invoice(session, rental, total=3000, lines=[["Tolls and fuel", 3000]], key="d")
    session.commit()
    row = api_client.get("/api/invoices/ledger").json()["rows"][0]
    assert row["turo_tolls_cents"] == 1200
    assert row["turo_tickets_cents"] == 5000
    assert row["turo_distance_cents"] == 4000
    assert row["turo_other_cents"] == 14800
    assert row["turo_unknown_cents"] == 777 + 3000, "no lines, and tolls mixed with fuel"


@requires_db
def test_every_rental_on_the_account_is_in_the_ledger(api_client, session, car, rental) -> None:
    """Including the ones with no crossings — a rental Turo charged for a ticket
    is still part of where the money went."""
    first = _crossing(session, car, rental, at=ENDS - td(days=1))
    quiet = Trip(
        vehicle_id=car.id, turo_trip_id="1001", guest_name="Quiet",
        starts_at=ENDS - td(days=30), ends_at=ENDS - td(days=28),
        state=TripState.completed, source=TripSource.email,
    )
    cancelled = Trip(
        vehicle_id=car.id, turo_trip_id="1002", guest_name="Cancelled",
        starts_at=ENDS - td(days=20), ends_at=ENDS - td(days=19),
        state=TripState.cancelled, source=TripSource.email,
    )
    upcoming = Trip(
        vehicle_id=car.id, turo_trip_id="1003", guest_name="Upcoming",
        starts_at=datetime.now(UTC) + td(days=3), ends_at=datetime.now(UTC) + td(days=5),
        state=TripState.upcoming, source=TripSource.email,
    )
    session.add_all([quiet, cancelled, upcoming])
    session.flush()
    _turo_invoice(session, quiet, total=5000, lines=[["Tickets", 5000]])
    session.commit()

    rows = api_client.get("/api/invoices/ledger").json()["rows"]
    names = {r["guest_name"]: r for r in rows}
    assert set(names) == {"Dylan", "Quiet"}, "not cancelled, not yet started"
    assert names["Quiet"]["tolls_cents"] == 0
    assert names["Quiet"]["turo_tickets_cents"] == 5000
    assert names["Quiet"]["charged_cents"] == 5000
    assert first.trip_id == rental.id


@requires_db
def test_a_rental_outside_every_statement_says_so(api_client, session, car, rental) -> None:
    """The operator's next job: pull the E-ZPass range that covers it."""
    _crossing(session, car, rental, at=ENDS - td(days=1))
    _crossing(session, car, rental, at=ENDS - td(hours=2))
    inside = Trip(
        vehicle_id=car.id, turo_trip_id="1001", guest_name="Inside",
        starts_at=ENDS - td(hours=20), ends_at=ENDS - td(hours=10),
        state=TripState.completed, source=TripSource.email,
    )
    before = Trip(
        vehicle_id=car.id, turo_trip_id="1002", guest_name="Before",
        starts_at=ENDS - td(days=40), ends_at=ENDS - td(days=38),
        state=TripState.completed, source=TripSource.email,
    )
    session.add_all([inside, before])
    session.commit()
    out = api_client.get("/api/invoices/ledger").json()
    names = {r["guest_name"]: r for r in out["rows"]}
    assert names["Inside"]["state"] == "no crossings"
    assert names["Before"]["state"] == "no statement yet"
    assert "outside the imported statements" in names["Before"]["note"]
    assert out["statements_from"] is not None and out["statements_to"] is not None


@requires_db
def test_with_no_statement_at_all_nothing_reads_as_crossing_free(
    api_client, session, rental
) -> None:
    out = api_client.get("/api/invoices/ledger").json()
    assert [r["state"] for r in out["rows"]] == ["no statement yet"]
    assert "no E-ZPass statement" in out["rows"][0]["note"]
