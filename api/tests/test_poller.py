"""The poll that keeps the run sheet current.

The claim worth testing is not that syncing works — that is covered elsewhere —
but that reconciling recovers a car whose parked fix was recorded before the
parking logic ever saw it, and that doing so repeatedly does not pile up
sessions or tasks.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from turonomics_api.db.models import (
    AspRule,
    ParkingSession,
    RuleSource,
    StreetSegmentSide,
    StreetSide,
    Task,
    TelemetryEvent,
    Vehicle,
)
from turonomics_api.ingest.poller import (
    DEFAULT_INTERVAL_MINUTES,
    interval_minutes,
    reconcile_engine_state,
)

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL") and not os.environ.get("DATABASE_URL"),
    reason="no database configured",
)

# Where the 4Runner actually sits, on the north side of Bergen St.
LAT, LON = 40.679865, -73.970204
PARKED_AT = datetime(2026, 9, 17, 16, 34, tzinfo=UTC)


def _bergen(session) -> StreetSegmentSide:
    seg = StreetSegmentSide(
        street_name="Bergen St",
        from_cross_street="Carlton Ave",
        to_cross_street="Vanderbilt Ave",
        side=StreetSide.north,
        geom="SRID=4326;LINESTRING(-73.9715 40.679900, -73.9690 40.679900)",
    )
    session.add(seg)
    session.flush()
    session.add(
        AspRule(
            segment_side_id=seg.id,
            days_of_week=[1, 4],
            starts_at=datetime(2026, 1, 1, 11, 30, tzinfo=UTC).timetz().replace(tzinfo=None),
            ends_at=datetime(2026, 1, 1, 13, 0, tzinfo=UTC).timetz().replace(tzinfo=None),
            source=RuleSource.nyc_signs,
            confidence=1.0,
        )
    )
    session.flush()
    return seg


def _parked_car(session, *, is_running: bool = False, at: datetime = PARKED_AT) -> Vehicle:
    v = Vehicle(nickname="Jimmy", make="Toyota", model="4-Runner", year=2023, plate="LEH9892")
    session.add(v)
    session.flush()
    session.add(
        TelemetryEvent(
            vehicle_id=v.id,
            event_type="statsSnapshot",
            occurred_at=at,
            location=f"SRID=4326;POINT({LON} {LAT})",
            is_running=is_running,
            payload={},
            provider_event_id=f"stats:{at.isoformat()}",
        )
    )
    session.commit()
    return v


def test_a_fix_recorded_before_the_parking_logic_still_gets_a_session(session):
    """The gap this exists to close. The event is already stored, so no sync
    will ever re-deliver it; without reconciling, the car has no deadline until
    it next moves."""
    _bergen(session)
    v = _parked_car(session)
    assert session.scalar(select(func.count()).select_from(ParkingSession)) == 0

    assert reconcile_engine_state(session) == 1
    session.commit()

    ps = session.scalar(select(ParkingSession).where(ParkingSession.vehicle_id == v.id))
    assert ps is not None
    assert ps.ended_at is None
    # The clock starts when the car parked, not when we noticed.
    assert ps.started_at == PARKED_AT


def test_polling_repeatedly_does_not_pile_up_sessions_or_tasks(session):
    """It runs every ten minutes forever, so non-accumulation is the property
    that matters most."""
    _bergen(session)
    _parked_car(session)
    for _ in range(5):
        reconcile_engine_state(session)
        session.commit()

    assert session.scalar(select(func.count()).select_from(ParkingSession)) == 1
    assert session.scalar(select(func.count()).select_from(Task)) <= 1


def test_a_car_that_has_driven_off_has_its_session_closed(session):
    _bergen(session)
    v = _parked_car(session)
    reconcile_engine_state(session)
    session.commit()

    moving = PARKED_AT + timedelta(hours=2)
    session.add(
        TelemetryEvent(
            vehicle_id=v.id,
            event_type="statsSnapshot",
            occurred_at=moving,
            location=f"SRID=4326;POINT({LON} {LAT})",
            is_running=True,
            payload={},
            provider_event_id=f"stats:{moving.isoformat()}",
        )
    )
    session.commit()

    reconcile_engine_state(session)
    session.commit()

    ps = session.scalar(select(ParkingSession).where(ParkingSession.vehicle_id == v.id))
    assert ps.ended_at is not None


def test_a_vehicle_with_no_telemetry_is_skipped_not_guessed_at(session):
    session.add(Vehicle(nickname="No tracker", make="Ford", model="Transit", year=2024))
    session.commit()
    assert reconcile_engine_state(session) == 0
    assert session.scalar(select(func.count()).select_from(ParkingSession)) == 0


def test_silence_about_engine_state_opens_nothing(session):
    """``is_running`` of None means the provider did not say. Treating that as
    parked would start a deadline the operator never earned."""
    _bergen(session)
    v = _parked_car(session)
    session.execute(
        TelemetryEvent.__table__.update()
        .where(TelemetryEvent.vehicle_id == v.id)
        .values(is_running=None)
    )
    session.commit()
    assert reconcile_engine_state(session) == 0


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, DEFAULT_INTERVAL_MINUTES),
        ("", DEFAULT_INTERVAL_MINUTES),
        ("5", 5),
        ("0", 0),  # the off switch
        ("-3", 0),
        ("banana", DEFAULT_INTERVAL_MINUTES),  # junk must not silently stop polling
    ],
)
def test_interval_parsing(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv("SYNC_INTERVAL_MINUTES", raising=False)
    else:
        monkeypatch.setenv("SYNC_INTERVAL_MINUTES", raw)
    assert interval_minutes() == expected


# ---------------------------------------------------------------------------
# The external trigger
# ---------------------------------------------------------------------------


def test_the_sync_endpoint_is_shut_when_no_token_is_configured(client, monkeypatch):
    """Forgetting to set a token must close the door, not leave it open."""
    monkeypatch.delenv("SYNC_TOKEN", raising=False)
    assert client.post("/api/sync").status_code == 503


def test_the_sync_endpoint_rejects_a_wrong_token(client, monkeypatch):
    monkeypatch.setenv("SYNC_TOKEN", "the-real-token")
    assert client.post("/api/sync").status_code == 401
    assert (
        client.post("/api/sync", headers={"Authorization": "Bearer wrong"}).status_code == 401
    )
    # An empty bearer is not a pass.
    assert client.post("/api/sync", headers={"Authorization": "Bearer "}).status_code == 401
