"""Tests for the commands the site queues for the browser extension.

The cases worth testing hard are the ones where a click on the site could bill
a guest twice: a filing queued for a rental the ledger would hold back, a
double click, two browsers claiming one command, and a filing that was claimed
and never answered.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from datetime import timedelta as td

import pytest

from turonomics_api.db.models import (
    ExtensionCheckin,
    ExtensionCommand,
    ReimbursementInvoice,
    Toll,
    Trip,
    TripSource,
    TripState,
    Vehicle,
)
from turonomics_api.routers import commands as commands_router

from .conftest import requires_db

NOW = datetime.now(UTC)


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
    )
    session.add(trip)
    session.flush()
    session.add(
        Toll(
            vehicle_id=car.id, trip_id=trip.id, occurred_at=trip.ends_at - td(hours=5),
            plaza="CRZ", amount_cents=4071, fingerprint=f"t-{uuid.uuid4()}",
        )
    )
    session.commit()
    return trip


def _queue(api_client, kind="file", trip=None):
    body = {"kind": kind}
    if trip is not None:
        body["trip_id"] = str(trip.id)
    return api_client.post("/api/commands", json=body)


# ---------------------------------------------------------------------------
# Queueing
# ---------------------------------------------------------------------------


@requires_db
def test_a_filing_is_queued_for_a_rental_the_ledger_would_file(api_client, rental) -> None:
    out = _queue(api_client, trip=rental)
    assert out.status_code == 200, out.text
    body = out.json()
    assert body["state"] == "queued"
    assert body["turo_trip_id"] == "59077848" and body["guest_name"] == "Austin"


@requires_db
def test_a_filing_the_ledger_holds_back_is_refused_with_its_reason(
    api_client, session, rental
) -> None:
    """The site's File button must not be a way around next-draft's rule.
    Austin, before Turo's invoice was read: $140.40 nobody could break down."""
    session.add(
        ReimbursementInvoice(
            fingerprint="res:59077848:14040", reservation_id="59077848", state="charged",
            total_cents=14040, lines=[], trip_id=rental.id, last_seen_at=NOW, charged_at=NOW,
        )
    )
    session.commit()
    out = _queue(api_client, trip=rental)
    assert out.status_code == 409
    assert "$140.40" in out.json()["detail"]


@requires_db
def test_a_rental_already_asked_for_is_refused(api_client, session, rental) -> None:
    for toll in session.query(Toll).filter(Toll.trip_id == rental.id):
        toll.filed_at = NOW
    session.commit()
    out = _queue(api_client, trip=rental)
    assert out.status_code == 409


@requires_db
def test_a_double_click_is_one_command(api_client, rental) -> None:
    """The click that would otherwise file Austin's tolls twice."""
    first = _queue(api_client, trip=rental).json()
    second = _queue(api_client, trip=rental).json()
    assert first["id"] == second["id"]


@requires_db
def test_a_finished_filing_does_not_absorb_the_next_request(
    api_client, session, rental
) -> None:
    first = _queue(api_client, trip=rental).json()
    command = session.get(ExtensionCommand, uuid.UUID(first["id"]))
    command.state = "failed"
    session.commit()
    again = _queue(api_client, trip=rental).json()
    assert again["id"] != first["id"]


@requires_db
def test_a_pull_is_one_at_a_time_too(api_client) -> None:
    assert _queue(api_client, "pull").json()["id"] == _queue(api_client, "pull").json()["id"]


@requires_db
def test_nonsense_is_refused(api_client, rental) -> None:
    assert _queue(api_client, "delete-everything").status_code == 422
    assert _queue(api_client, "file").status_code == 422, "which rental?"
    assert _queue(api_client, "pull", trip=rental).status_code == 422
    assert api_client.post(
        "/api/commands", json={"kind": "file", "trip_id": str(uuid.uuid4())}
    ).status_code == 404


@requires_db
def test_queueing_needs_the_token(api_client, monkeypatch, rental) -> None:
    monkeypatch.setenv("TOLLS_TOKEN", "s3cret")
    assert _queue(api_client, "pull").status_code == 401
    ok = api_client.post(
        "/api/commands", json={"kind": "pull"}, headers={"Authorization": "Bearer s3cret"}
    )
    assert ok.status_code == 200


# ---------------------------------------------------------------------------
# Claiming and reporting
# ---------------------------------------------------------------------------


@requires_db
def test_the_oldest_command_is_claimed_and_only_once(api_client, rental) -> None:
    filing = _queue(api_client, trip=rental).json()
    pull = _queue(api_client, "pull").json()
    first = api_client.post("/api/commands/claim").json()
    second = api_client.post("/api/commands/claim").json()
    assert first["id"] == filing["id"] and first["state"] == "running"
    assert second["id"] == pull["id"]
    assert api_client.post("/api/commands/claim").status_code == 204


@requires_db
def test_claiming_is_the_check_in(api_client, session) -> None:
    api_client.post("/api/commands/claim", headers={"X-Extension-Version": "1.10.0"})
    out = api_client.get("/api/commands").json()
    assert out["listening"] is True
    assert out["extension_version"] == "1.10.0"


@requires_db
def test_a_quiet_extension_is_not_listening(api_client, session) -> None:
    session.add(ExtensionCheckin(id=1, seen_at=NOW - td(minutes=5), version="1.10.0"))
    session.commit()
    assert api_client.get("/api/commands").json()["listening"] is False


@requires_db
def test_a_new_version_is_recorded_at_once(api_client, session) -> None:
    """The check-in is written at most every twenty seconds, except when the
    version changes — which is the moment someone is checking it took."""
    api_client.post("/api/commands/claim", headers={"X-Extension-Version": "1.9.0"})
    api_client.post("/api/commands/claim", headers={"X-Extension-Version": "1.10.0"})
    assert api_client.get("/api/commands").json()["extension_version"] == "1.10.0"


@requires_db
def test_the_answer_is_recorded(api_client, rental) -> None:
    _queue(api_client, trip=rental)
    claimed = api_client.post("/api/commands/claim").json()
    out = api_client.post(
        f"/api/commands/{claimed['id']}/done",
        json={"ok": True, "result": "Filed for $40.71 to Austin on reservation 59077848"},
    ).json()
    assert out["state"] == "done" and out["result"].startswith("Filed for $40.71")
    listed = api_client.get("/api/commands").json()["commands"][0]
    assert listed["state"] == "done"


@requires_db
def test_a_failure_is_recorded_as_one(api_client) -> None:
    _queue(api_client, "pull")
    claimed = api_client.post("/api/commands/claim").json()
    out = api_client.post(
        f"/api/commands/{claimed['id']}/done", json={"ok": False, "result": "Open Turo"}
    ).json()
    assert out["state"] == "failed"


@requires_db
def test_an_unclaimed_command_cannot_be_reported(api_client) -> None:
    queued = _queue(api_client, "pull").json()
    out = api_client.post(f"/api/commands/{queued['id']}/done", json={"ok": True, "result": "x"})
    assert out.status_code == 409


@requires_db
def test_a_filing_that_never_answered_is_abandoned_not_retried(
    api_client, session, rental
) -> None:
    """It may have filed and lost the answer. Running it again is a guest
    asked twice, so it is left for a person — and says so."""
    _queue(api_client, trip=rental)
    claimed = api_client.post("/api/commands/claim").json()
    command = session.get(ExtensionCommand, uuid.UUID(claimed["id"]))
    command.claimed_at = NOW - commands_router.STALE_AFTER - td(minutes=1)
    session.commit()

    assert api_client.post("/api/commands/claim").status_code == 204, "not re-run"
    listed = api_client.get("/api/commands").json()["commands"][0]
    assert listed["state"] == "abandoned"
    assert "check Turo" in listed["result"]


@requires_db
def test_a_late_answer_still_counts(api_client, session, rental) -> None:
    """A late answer is still the truth about what happened."""
    _queue(api_client, trip=rental)
    claimed = api_client.post("/api/commands/claim").json()
    command = session.get(ExtensionCommand, uuid.UUID(claimed["id"]))
    command.claimed_at = NOW - commands_router.STALE_AFTER - td(minutes=1)
    session.commit()
    api_client.get("/api/commands")
    out = api_client.post(
        f"/api/commands/{claimed['id']}/done", json={"ok": True, "result": "Filed"}
    ).json()
    assert out["state"] == "done"


@requires_db
def test_claiming_and_reporting_need_the_token(api_client, monkeypatch) -> None:
    monkeypatch.setenv("TOLLS_TOKEN", "s3cret")
    assert api_client.post("/api/commands/claim").status_code == 401
    assert api_client.post(
        f"/api/commands/{uuid.uuid4()}/done", json={"ok": True, "result": "x"}
    ).status_code == 401


# ---------------------------------------------------------------------------
# The draft the extension files from
# ---------------------------------------------------------------------------


@requires_db
def test_a_draft_says_whether_the_ledger_would_file_it(api_client, session, rental) -> None:
    """The extension checks again when it runs: the ledger may have changed
    between the click and the claim."""
    ok = api_client.get(f"/api/invoices/{rental.id}/draft").json()
    assert ok["fileable"] is True and ok["held_because"] is None
    session.add(
        ReimbursementInvoice(
            fingerprint="res:59077848:14040", reservation_id="59077848", state="charged",
            total_cents=14040, lines=[], trip_id=rental.id, last_seen_at=NOW, charged_at=NOW,
        )
    )
    session.commit()
    held = api_client.get(f"/api/invoices/{rental.id}/draft").json()
    assert held["fileable"] is False and "$140.40" in held["held_because"]
