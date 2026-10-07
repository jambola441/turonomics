"""Tests for the trip view: one rental, Turo's side and ours.

What matters is that it is the whole picture — Turo's reservation detail as it
was pulled, its invoices with their status, our crossings and what became of
them — and that it is behind the token, because Turo's detail carries the
guest's name and the pickup address.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from datetime import timedelta as td

import pytest

from turonomics_api.db.models import (
    ExtensionCommand,
    ReimbursementInvoice,
    Toll,
    Trip,
    TripSource,
    TripState,
    Vehicle,
)
from turonomics_api.routers.trips import turo_facts

from .conftest import requires_db

NOW = datetime.now(UTC)


def _moment(when: datetime) -> dict:
    return {
        "epochMillis": int(when.timestamp() * 1000),
        "localDate": f"{when:%Y-%m-%d}",
        "localTime": f"{when:%H:%M}",
    }


DETAIL = {
    "id": 59077848,
    "statusCode": "COMPLETED",
    "renter": {"name": "Austin", "firstName": "Austin"},
    "created": {"epochMillis": 1, "localDate": "2026-07-01", "localTime": "09:12"},
    "booking": {
        "start": {"epochMillis": 1, "localDate": "2026-07-16", "localTime": "17:00"},
        "end": {"epochMillis": 2, "localDate": "2026-07-19", "localTime": "16:30"},
        "costWithCurrency": {"amount": 212.5, "currencyCode": "USD"},
        "distanceLimit": {"scalar": 600, "unit": "MI", "unlimited": False},
        "location": {"address": "1 Example St, Brooklyn, NY"},
        "vehicleRegistration": {"licensePlate": "LWH4685", "state": "NY"},
    },
    "odometerDetail": {
        "checkInOdometerReading": {"scalar": 48219, "unit": "MI", "unlimited": False},
        "checkOutOdometerReading": None,
        "excessDistance": {"scalar": 156, "unit": "MI", "unlimited": False},
    },
    "distanceOverageFee": {
        "distance": {"scalar": 1, "unit": "MI", "unlimited": False},
        "money": {"amount": 1.0, "currencyCode": "USD"},
    },
    "protectionLevel": "PREMIUM",
    "instantBookable": True,
    "reservationActions": ["VIEW_INVOICE_HUB", "UPLOAD_TRIP_PHOTOS"],
}


def test_the_facts_are_what_a_person_reads_first() -> None:
    facts = {f.label: f.value for f in turo_facts(DETAIL)}
    assert facts["Status"] == "COMPLETED"
    assert facts["Trip price"] == "$212.50"
    assert facts["Distance included"] == "600 mi"
    assert facts["Odometer at check-in"] == "48,219 mi"
    assert facts["Over the limit"] == "156 mi"
    assert facts["Overage rate"] == "$1.00 per 1 mi"
    assert facts["Starts"] == "2026-07-16 17:00"
    assert facts["Instant book"] == "yes"
    assert facts["Turo offers"] == "VIEW_INVOICE_HUB, UPLOAD_TRIP_PHOTOS"
    assert "Odometer at check-out" not in facts, "null is not said"


def test_a_rental_never_pulled_has_no_facts() -> None:
    assert turo_facts(None) == []
    assert turo_facts({}) == []


def test_unlimited_distance_says_so() -> None:
    facts = {f.label: f.value for f in turo_facts(
        {"booking": {"distanceLimit": {"scalar": None, "unit": "MI", "unlimited": True}}}
    )}
    assert facts["Distance included"] == "unlimited"


@pytest.fixture(autouse=True)
def _open(monkeypatch):
    monkeypatch.delenv("TOLLS_TOKEN", raising=False)


@pytest.fixture()
def car(session):
    vehicle = Vehicle(nickname="Jimmy", make="Toyota", model="Corolla", year=2024, plate="LWH4685")
    session.add(vehicle)
    session.flush()
    return vehicle


@pytest.fixture()
def rental(session, car):
    trip = Trip(
        vehicle_id=car.id, turo_trip_id="59077848", guest_name="Austin",
        starts_at=NOW - td(days=23), ends_at=NOW - td(days=20),
        state=TripState.completed, source=TripSource.email,
        turo_detail=DETAIL, detail_synced_at=NOW,
    )
    session.add(trip)
    session.flush()
    return trip


@requires_db
def test_the_view_has_both_sides(api_client, session, car, rental) -> None:
    session.add(
        Toll(
            vehicle_id=car.id, trip_id=rental.id, occurred_at=rental.ends_at - td(hours=5),
            plaza="CRZ", amount_cents=4071, fingerprint=f"t-{uuid.uuid4()}", filed_at=NOW,
        )
    )
    session.add(
        ReimbursementInvoice(
            fingerprint="res:59077848:14040", reservation_id="59077848",
            turo_invoice_id="113672232", state="charged", total_cents=14040,
            lines=[["Additional distance", 15600]], toll_cents=None, trip_id=rental.id,
            last_seen_at=NOW, charged_at=NOW,
            turo_body={"invoiceId": 113672232, "reimbursementStatus": "ACCEPTED"},
        )
    )
    session.add(
        ExtensionCommand(
            kind="file", trip_id=rental.id, state="done", requested_at=NOW,
            result="Filed for $40.71 to Austin",
        )
    )
    session.commit()

    view = api_client.get(f"/api/trips/{rental.id}/view").json()
    assert view["turo_trip_id"] == "59077848" and view["plate"] == "LWH4685"
    assert view["reservation_url"] == "https://turo.com/us/en/reservation/59077848"
    assert view["invoice_hub_url"].endswith("/reservation/59077848/invoice-hub")
    assert {"label": "Trip price", "value": "$212.50"} in view["turo_facts"]
    assert view["turo_detail"]["protectionLevel"] == "PREMIUM", "the whole of it"

    [toll] = view["tolls"]
    assert toll["amount_cents"] == 4071 and toll["filed_at"] is not None

    [invoice] = view["invoices"]
    assert invoice["turo_status"] == "ACCEPTED"
    assert invoice["lines"] == [{"label": "Additional distance", "value": "$156.00"}]
    assert invoice["url"].endswith("reimbursement/invoice?invoiceId=113672232")

    assert view["ledger"]["state"] == "awaiting payment"
    assert view["ledger"]["turo_distance_cents"] == 15600
    assert view["fileable"] is False and view["held_because"]
    assert view["commands"][0]["result"] == "Filed for $40.71 to Austin"


@requires_db
def test_an_invoice_known_only_by_the_extensions_id_gets_no_link(
    api_client, session, rental
) -> None:
    """Before it is read off Turo, the id it carries may be the reimbursement
    id, and a link built from that would open the wrong invoice or none."""
    session.add(
        ReimbursementInvoice(
            fingerprint="inv:11608865", reservation_id="59077848", turo_invoice_id="11608865",
            state="filed", total_cents=4071, lines=[["Tolls", 4071]], toll_cents=4071,
            trip_id=rental.id, last_seen_at=NOW,
        )
    )
    session.commit()
    [invoice] = api_client.get(f"/api/trips/{rental.id}/view").json()["invoices"]
    assert invoice["url"] is None


@requires_db
def test_a_rental_to_bill_can_be_filed_from_its_view(api_client, session, car, rental) -> None:
    session.add(
        Toll(
            vehicle_id=car.id, trip_id=rental.id, occurred_at=rental.ends_at - td(hours=5),
            plaza="CRZ", amount_cents=4071, fingerprint=f"t-{uuid.uuid4()}",
        )
    )
    session.commit()
    view = api_client.get(f"/api/trips/{rental.id}/view").json()
    assert view["fileable"] is True and view["held_because"] is None


@requires_db
def test_an_off_platform_rental_has_a_view_too(api_client, session, car) -> None:
    created = api_client.post(
        "/api/trips",
        json={"vehicle": "Jimmy", "guest_name": "Cousin Vinny",
              "starts_at": "2026-09-01T10:00", "ends_at": "2026-09-03T18:00",
              "earnings_cents": 30000},
    ).json()["trip"]
    view = api_client.get(f"/api/trips/{created['id']}/view").json()
    assert view["trip"]["source"] == "manual"
    assert view["turo_trip_id"] is None and view["reservation_url"] is None
    assert view["turo_facts"] == []
    assert view["fileable"] is False and "off-platform" in view["held_because"]


@requires_db
def test_the_list_carries_turo_rentals_and_off_platform_ones(api_client, session, rental) -> None:
    api_client.post(
        "/api/trips",
        json={"vehicle": "Jimmy", "starts_at": "2026-09-01T10:00", "ends_at": "2026-09-03T18:00"},
    )
    trips = api_client.get("/api/trips?manual_only=false").json()["trips"]
    sources = {t["source"] for t in trips}
    assert sources == {"manual", "email"}
    turo = next(t for t in trips if t["source"] == "email")
    assert turo["turo_trip_id"] == "59077848" and turo["state"] == "completed"


@requires_db
def test_the_view_needs_the_token(api_client, monkeypatch, rental) -> None:
    """Turo's detail names the guest and the pickup address; the lists are
    public, this is not."""
    monkeypatch.setenv("TOLLS_TOKEN", "s3cret")
    assert api_client.get(f"/api/trips/{rental.id}/view").status_code == 401
    ok = api_client.get(
        f"/api/trips/{rental.id}/view", headers={"Authorization": "Bearer s3cret"}
    )
    assert ok.status_code == 200


@requires_db
def test_an_unknown_rental_is_a_404(api_client) -> None:
    assert api_client.get(f"/api/trips/{uuid.uuid4()}/view").status_code == 404


@requires_db
def test_a_pull_keeps_turos_whole_detail(api_client, session, rental) -> None:
    rental.turo_detail = None
    session.commit()
    body = {**DETAIL, "somethingNew": {"nobody": "asked yet"}}
    out = api_client.post("/api/turo/details", json={"details": [body]})
    assert out.status_code == 200, out.text
    session.refresh(rental)
    assert rental.turo_detail is not None
    assert rental.turo_detail["somethingNew"] == {"nobody": "asked yet"}


@requires_db
def test_reading_an_invoice_keeps_turos_whole_page(api_client, session, rental) -> None:
    body = {
        "invoiceId": 113672232, "reimbursementId": 1, "reimbursementStatus": "ACCEPTED",
        "lineItems": [{"type": "ADDITIONAL_DISTANCE", "title": "Additional distance",
                       "total": {"amount": 156.0, "currencyCode": "USD"}}],
        "total": {"amount": 156.0, "currencyCode": "USD"},
    }
    api_client.post(
        "/api/turo/invoices", json={"invoices": [{"reservation_id": "59077848", "body": body}]}
    )
    row = session.query(ReimbursementInvoice).one()
    assert row.turo_body is not None and row.turo_body["reimbursementStatus"] == "ACCEPTED"
