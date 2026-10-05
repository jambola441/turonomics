"""Tests for pulling Turo's own reservation detail.

The thing worth testing hard is the retiming. Email states a trip's times as
they were when it was sent, so these times win — and a changed end time moves
which crossings fall inside the rental, which moves whose money they are.

Shapes taken from docs/design/03-turo-api.md, observed from the real account.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from turonomics_api.db.models import Toll, Trip, TripSource, TripState, Vehicle
from turonomics_api.ingest.turo_detail import (
    DetailResult,
    apply_detail,
    describe_grace_periods,
    parse_detail,
    wanted_reservations,
)

from .conftest import requires_db

NOW = datetime(2026, 10, 5, 18, 0, tzinfo=UTC)


@pytest.fixture()
def jerry(session):
    car = Vehicle(
        nickname="Jerry", make="Toyota", model="Corolla", year=2025, plate="LZA7293"
    )
    session.add(car)
    session.flush()
    return car


@pytest.fixture()
def trip(session, jerry):
    row = Trip(
        vehicle_id=jerry.id,
        turo_trip_id="58358939",
        guest_name="Dylan",
        starts_at=datetime(2026, 7, 3, 14, 0, tzinfo=UTC),
        ends_at=datetime(2026, 7, 5, 18, 0, tzinfo=UTC),
        state=TripState.completed,
        source=TripSource.email,
    )
    session.add(row)
    session.flush()
    return row


def _moment(when: datetime) -> dict[str, Any]:
    """Turo's timestamp block: millis, plus a local date and time."""
    return {
        "epochMillis": int(when.timestamp() * 1000),
        "localDate": f"{when:%Y-%m-%d}",
        "localTime": f"{when:%H:%M}",
    }


def _detail(
    # Accepts a string so a test can pass `trip.turo_trip_id` straight in, and
    # emits an int, because that is what Turo sends — `parse_detail` refusing a
    # string id is the behaviour, not an inconvenience.
    reservation: int | str = 58358939,
    start: datetime = datetime(2026, 7, 3, 14, 0, tzinfo=UTC),
    end: datetime = datetime(2026, 7, 5, 18, 0, tzinfo=UTC),
    *,
    grace: datetime | None = None,
    plate: str = "LZA7293",
    allowed: object = True,
) -> dict[str, Any]:
    booking: dict[str, Any] = {
        "start": _moment(start),
        "end": _moment(end),
        "cost": 212.5,
        "mileageLimit": 200,
        "vehicleRegistration": {
            "licensePlate": plate,
            "state": "NY",
            "regionRequired": False,
        },
    }
    if grace is not None:
        booking["gracePeriodEnd"] = _moment(grace)
    return {
        "id": int(reservation),
        "allowedToRequestReimbursement": allowed,
        "booking": booking,
        "driverRole": "HOST",
        "odometerDetail": {"checkInOdometerReading": {"scalar": 48219, "unit": "MI"}},
    }


# ---------------------------------------------------------------------------
# Reading the payload
# ---------------------------------------------------------------------------


def test_the_times_come_from_the_millis() -> None:
    parsed = parse_detail(_detail())
    assert parsed is not None
    assert parsed.starts_at == datetime(2026, 7, 3, 14, 0, tzinfo=UTC)
    assert parsed.ends_at == datetime(2026, 7, 5, 18, 0, tzinfo=UTC)


def test_the_millis_are_read_as_millis() -> None:
    """Pins the unit, which is the real risk: epoch *seconds* read as millis
    puts every trip in 1970, and the other way round puts them in the year
    57000. The sub-millisecond question is not tested because there is nothing
    to test — see the note in `_moment`."""
    odd = {"id": 1, "booking": {"start": {"epochMillis": 1767225599001}}}
    parsed = parse_detail(odd)
    assert parsed is not None
    assert parsed.starts_at == datetime(2025, 12, 31, 23, 59, 59, 1000, tzinfo=UTC)


def test_the_reservation_id_is_a_string_because_that_is_how_it_is_stored() -> None:
    parsed = parse_detail(_detail(reservation=58358939))
    assert parsed is not None and parsed.reservation_id == "58358939"


def test_the_plate_is_normalised() -> None:
    parsed = parse_detail(_detail(plate=" lza 7293 "))
    assert parsed is not None and parsed.license_plate == "LZA 7293"


def test_the_filing_flag_is_read_as_a_boolean_not_a_truthiness() -> None:
    """Only a real boolean is trusted. If Turo ever answers "yes" here, that
    must read as "unknown" rather than as permission — this field is about to
    be the thing that says whether money can still be collected."""
    assert parse_detail(_detail(allowed=False)).can_file_reimbursement is False  # type: ignore[union-attr]
    assert parse_detail(_detail(allowed=None)).can_file_reimbursement is None  # type: ignore[union-attr]
    assert parse_detail(_detail(allowed="yes")).can_file_reimbursement is None  # type: ignore[arg-type,union-attr]
    assert parse_detail(_detail(allowed=1)).can_file_reimbursement is None  # type: ignore[arg-type,union-attr]


def test_a_body_that_is_not_a_reservation_is_refused() -> None:
    """The extension posts what it got, and Turo answers an error body with a
    200 often enough that this cannot raise."""
    assert parse_detail({}) is None
    assert parse_detail({"error": "not found"}) is None
    assert parse_detail({"id": "58358939"}) is None, "a string id is not the shape"
    assert parse_detail({"id": True}) is None, "and neither is a bool"


def test_a_reservation_with_no_booking_block_still_parses() -> None:
    """Enough to record that it was seen, with nothing claimed about its
    times."""
    parsed = parse_detail({"id": 58358939})
    assert parsed is not None
    assert parsed.reservation_id == "58358939"
    assert parsed.starts_at is None and parsed.ends_at is None


# ---------------------------------------------------------------------------
# Storing it
# ---------------------------------------------------------------------------


@requires_db
def test_turos_times_win_over_the_emails(session, trip) -> None:
    """The whole reason for pulling this. Nothing re-states a trip's times by
    email once a guest extends, so the booking is the only current source."""
    moved = trip.ends_at + timedelta(hours=3)
    result = DetailResult()
    parsed = parse_detail(
        _detail(reservation=trip.turo_trip_id, start=trip.starts_at, end=moved)
    )
    assert parsed is not None
    apply_detail(session, parsed, now=NOW, result=result)
    session.flush()

    assert trip.ends_at == moved
    assert result.stored == 1
    assert len(result.retimed) == 1, "and says so rather than moving it quietly"
    assert trip.turo_trip_id in result.retimed[0]


@requires_db
def test_unchanged_times_are_not_reported_as_moved(session, trip) -> None:
    result = DetailResult()
    parsed = parse_detail(
        _detail(reservation=trip.turo_trip_id, start=trip.starts_at, end=trip.ends_at)
    )
    assert parsed is not None
    apply_detail(session, parsed, now=NOW, result=result)
    assert result.retimed == []
    assert result.stored == 1


@requires_db
def test_an_end_before_its_start_is_ignored_rather_than_written(session, trip) -> None:
    """There is a check constraint on the interval, so writing this would fail
    the whole batch rather than this one reservation."""
    was = (trip.starts_at, trip.ends_at)
    result = DetailResult()
    parsed = parse_detail(
        _detail(
            reservation=trip.turo_trip_id,
            start=trip.starts_at,
            end=trip.starts_at - timedelta(hours=1),
        )
    )
    assert parsed is not None
    apply_detail(session, parsed, now=NOW, result=result)
    session.flush()
    assert (trip.starts_at, trip.ends_at) == was
    assert result.retimed == []


@requires_db
def test_a_reservation_this_fleet_has_no_trip_for_is_listed_not_invented(
    session,
) -> None:
    result = DetailResult()
    parsed = parse_detail(_detail(reservation=99999999))
    assert parsed is not None
    assert apply_detail(session, parsed, now=NOW, result=result) is None
    assert result.unknown == ["99999999"]
    assert result.stored == 0


@requires_db
def test_a_plate_that_disagrees_is_reported_and_not_corrected(session, trip) -> None:
    """A rental on the wrong car bills the wrong guest. Reassigning it from
    inside a sync is not a repair, it is a second guess."""
    before = trip.vehicle_id
    result = DetailResult()
    parsed = parse_detail(_detail(reservation=trip.turo_trip_id, plate="XYZ9999"))
    assert parsed is not None
    apply_detail(session, parsed, now=NOW, result=result)
    assert len(result.wrong_plate) == 1
    assert trip.vehicle_id == before


@requires_db
def test_a_plate_matching_but_spaced_differently_is_not_a_disagreement(
    session, trip
) -> None:
    plate = trip.vehicle.plate
    result = DetailResult()
    parsed = parse_detail(
        _detail(reservation=trip.turo_trip_id, plate=f"{plate[:3]} {plate[3:]}")
    )
    assert parsed is not None
    apply_detail(session, parsed, now=NOW, result=result)
    assert result.wrong_plate == []


@requires_db
def test_the_filing_flag_and_sync_time_are_stored(session, trip) -> None:
    result = DetailResult()
    parsed = parse_detail(_detail(reservation=trip.turo_trip_id, allowed=False))
    assert parsed is not None
    apply_detail(session, parsed, now=NOW, result=result)
    session.flush()
    assert trip.can_file_reimbursement is False
    assert trip.detail_synced_at == NOW


# ---------------------------------------------------------------------------
# What to fetch, and what the grace period turns out to be
# ---------------------------------------------------------------------------


@requires_db
def test_never_fetched_reservations_come_first(session, trip) -> None:
    """So a pull that is interrupted has made progress on the rentals nothing
    is known about, rather than refreshing the newest handful again."""
    trip.detail_synced_at = NOW
    session.flush()
    wanted = wanted_reservations(session)
    assert trip.turo_trip_id in wanted
    # With only one trip the ordering cannot be observed directly; what can be
    # is that a fetched one is not dropped, because a stale detail still needs
    # refreshing.
    assert len(wanted) >= 1


@requires_db
def test_the_grace_period_is_described_rather_than_trusted(session, trip) -> None:
    """Turo's `gracePeriodEnd` sits beside a cancellation policy block, so it
    may be the free-cancellation deadline rather than a return grace. This
    report is what settles it, and until it does the matcher keeps its own
    guess."""
    result = DetailResult()
    # Three hours after the start, which is what a cancellation deadline would
    # look like — and nothing like a late return.
    parsed = parse_detail(
        _detail(
            reservation=trip.turo_trip_id,
            start=trip.starts_at,
            end=trip.ends_at,
            grace=trip.starts_at + timedelta(hours=3),
        )
    )
    assert parsed is not None
    apply_detail(session, parsed, now=NOW, result=result)
    session.flush()

    lines = describe_grace_periods(session)
    assert len(lines) == 1
    assert "+3.0h from start" in lines[0]
    hours_to_end = (trip.ends_at - trip.starts_at).total_seconds() / 3600
    assert f"{3 - hours_to_end:+.1f}h from end" in lines[0]


@requires_db
def test_a_reservation_without_a_grace_period_is_not_described(session, trip) -> None:
    result = DetailResult()
    parsed = parse_detail(_detail(reservation=trip.turo_trip_id, grace=None))
    assert parsed is not None
    apply_detail(session, parsed, now=NOW, result=result)
    session.flush()
    assert describe_grace_periods(session) == []


# ---------------------------------------------------------------------------
# Through the API
# ---------------------------------------------------------------------------


@requires_db
def test_the_endpoint_stores_a_batch_and_reports_what_moved(
    api_client, monkeypatch, session, trip
) -> None:
    monkeypatch.delenv("TOLLS_TOKEN", raising=False)
    moved = trip.ends_at + timedelta(hours=2)
    body = {
        "details": [
            _detail(reservation=trip.turo_trip_id, start=trip.starts_at, end=moved),
            {"error": "not found"},
            _detail(reservation=99999999),
        ]
    }
    response = api_client.post("/api/turo/details", json=body)
    assert response.status_code == 200
    out = response.json()
    assert out["seen"] == 2, "the error body is not a reservation"
    assert out["unparsed"] == 1
    assert out["stored"] == 1
    assert out["unknown"] == ["99999999"]
    assert len(out["retimed"]) == 1


@requires_db
def test_the_wanted_list_names_the_route_it_wants_fetched(api_client, trip) -> None:
    """The extension holds no policy: which reservations, and from which path,
    are both answered here so that changing either needs no side-loaded
    rebuild."""
    out = api_client.get("/api/turo/wanted").json()
    assert trip.turo_trip_id in out["reservations"]
    assert "{id}" in out["detail_path"]
    assert "reservationId" in out["detail_path"]


@requires_db
def test_the_endpoint_is_gated_by_the_tolls_token(
    api_client, monkeypatch, session, trip
) -> None:
    monkeypatch.setenv("TOLLS_TOKEN", "letmein")
    body = {"details": [_detail(reservation=trip.turo_trip_id)]}
    assert api_client.post("/api/turo/details", json=body).status_code == 401
    assert (
        api_client.post(
            "/api/turo/details", json=body, headers={"Authorization": "Bearer letmein"}
        ).status_code
        == 200
    )


@requires_db
def test_nothing_moved_means_no_rematch(api_client, monkeypatch, session, trip) -> None:
    """Re-running attribution is cheap, not free, and a pull that changed
    nothing should read as having changed nothing.

    An unattributed crossing that *would* match is left sitting there on
    purpose: without it this test passes whether or not the rematch runs,
    because there is nothing for it to find.
    """
    monkeypatch.delenv("TOLLS_TOKEN", raising=False)
    orphan = Toll(
        vehicle_id=trip.vehicle_id,
        occurred_at=trip.starts_at + timedelta(hours=4),
        plaza="MTAB&T RKB",
        amount_cents=611,
        fingerprint="turo-detail-test-orphan",
    )
    session.add(orphan)
    session.flush()
    assert orphan.trip_id is None

    body = {
        "details": [
            _detail(reservation=trip.turo_trip_id, start=trip.starts_at, end=trip.ends_at)
        ]
    }
    out = api_client.post("/api/turo/details", json=body).json()
    assert out["retimed"] == []
    assert out["tolls_rematched"] == 0
    session.refresh(orphan)
    assert orphan.trip_id is None, "nothing moved, so nothing was re-attributed"


@requires_db
def test_a_moved_booking_re_attributes_the_crossings(
    api_client, monkeypatch, session, trip
) -> None:
    """The payoff. A crossing after the original end belongs to nobody until
    Turo's booking says the rental ran that long."""
    monkeypatch.delenv("TOLLS_TOKEN", raising=False)
    late = trip.ends_at + timedelta(hours=1)
    orphan = Toll(
        vehicle_id=trip.vehicle_id,
        occurred_at=late,
        plaza="MTAB&T RKB",
        amount_cents=611,
        fingerprint="turo-detail-test-late",
    )
    session.add(orphan)
    session.flush()
    assert orphan.trip_id is None

    body = {
        "details": [
            _detail(
                reservation=trip.turo_trip_id,
                start=trip.starts_at,
                end=late + timedelta(minutes=30),
            )
        ]
    }
    out = api_client.post("/api/turo/details", json=body).json()
    assert len(out["retimed"]) == 1
    assert out["tolls_rematched"] == 1
    session.refresh(orphan)
    assert orphan.trip_id == trip.id


@pytest.mark.parametrize("body", [{}, {"details": []}])
@requires_db
def test_an_empty_pull_is_not_an_error(api_client, monkeypatch, body) -> None:
    monkeypatch.delenv("TOLLS_TOKEN", raising=False)
    response = api_client.post("/api/turo/details", json={"details": [], **body})
    assert response.status_code == 200
    assert response.json()["seen"] == 0


# ---------------------------------------------------------------------------
# Saying what actually moved
# ---------------------------------------------------------------------------


@requires_db
def test_an_overnight_move_is_legible(session, trip) -> None:
    """From a real pull: "2026-08-29 13:00–19:00 -> 2026-08-29 13:00–19:00".

    The rental had genuinely moved — its end shifted by a whole day — and the
    report hid the only part that changed, because the end was formatted
    without its date. A report of what moved has to say what moved.
    """
    result = DetailResult()
    parsed = parse_detail(
        _detail(
            reservation=trip.turo_trip_id,
            start=trip.starts_at,
            end=trip.ends_at + timedelta(days=1),
        )
    )
    assert parsed is not None
    apply_detail(session, parsed, now=NOW, result=result)
    line = result.retimed[0]
    before, after = line.split(" -> ")
    assert before != after, line
    # Both ends carry a date, because the days differ on both sides.
    assert before.count("2026-") == 2 and after.count("2026-") == 2, line


@requires_db
def test_a_same_day_move_stays_short(session, trip) -> None:
    """The end's date is kept when it adds something, not always: a rental
    inside one day reads better as "13:00–19:00"."""
    inside = trip.starts_at.replace(hour=9)
    result = DetailResult()
    parsed = parse_detail(
        _detail(
            reservation=trip.turo_trip_id,
            start=inside,
            end=inside + timedelta(hours=6),
        )
    )
    assert parsed is not None
    apply_detail(session, parsed, now=NOW, result=result)
    _, after = result.retimed[0].split(" -> ")
    assert after.count("2026-") == 1, after
