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
def test_turos_own_answer_decides_whether_it_can_be_filed(
    api_client, session, car, rental
) -> None:
    """A day count cannot see a hold or a dispute. Where the pull has fetched
    Turo's answer, that is the one that counts — and the response says which
    of the two it gave."""
    _crossing(session, car, rental, at=ENDS - td(hours=3))
    rental.can_file_reimbursement = False
    session.commit()

    out = api_client.get(f"/api/invoices/{rental.id}/draft").json()
    assert out["can_file"] is False, "despite being inside the 90 days"
    assert out["can_file_from_turo"] is True
    assert out["days_left"] is not None and out["days_left"] > 0


@requires_db
def test_without_turos_answer_the_window_decides(
    api_client, session, car, rental
) -> None:
    _crossing(session, car, rental, at=ENDS - td(hours=3))
    session.commit()
    out = api_client.get(f"/api/invoices/{rental.id}/draft").json()
    assert out["can_file"] is True
    assert out["can_file_from_turo"] is False


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
