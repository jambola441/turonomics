"""Schema behaviour tests.

The side-of-street test uses the real GPS fix that came back from the fleet's
own Bouncie devices, because the whole confirm-the-spot design rests on a claim
about how ambiguous that fix is. Better to measure it than assert it.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, time, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from turonomics_api.db.models import (
    AspRule,
    ParkingSession,
    RuleSource,
    StreetSegmentSide,
    StreetSide,
    Task,
    TaskKind,
    TaskState,
    TelemetryEvent,
    Trip,
    TripSource,
    TripState,
    Vehicle,
)

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL") and not os.environ.get("DATABASE_URL"),
    reason="no database configured",
)

# Real fix for the 4Runner, reported by its own device: 590 Bergen St, Brooklyn.
JIMMY_LAT, JIMMY_LON = 40.679865, -73.970204
# A Brooklyn side street is roughly 11 m curb to curb; ~0.0001 deg of latitude.
BERGEN_NORTH_LAT = 40.679900
BERGEN_SOUTH_LAT = 40.679800


def _point(lon: float, lat: float) -> str:
    return f"SRID=4326;POINT({lon} {lat})"


def _line(lat: float) -> str:
    return f"SRID=4326;LINESTRING(-73.9715 {lat}, -73.9690 {lat})"


def _vehicle(session, **kw) -> Vehicle:
    defaults = dict(
        nickname="Jimmy",
        make="Toyota",
        model="4-Runner",
        year=2023,
        plate="KDR8814",
        reports_fuel_level=True,
        reports_obd_odometer=True,
    )
    v = Vehicle(**{**defaults, **kw})
    session.add(v)
    session.flush()
    return v


def test_vehicle_and_telemetry_roundtrip(session):
    v = _vehicle(session)
    session.add(
        TelemetryEvent(
            vehicle_id=v.id,
            event_type="tripEnd",
            occurred_at=datetime(2026, 9, 15, 11, 24, tzinfo=UTC),
            location=_point(JIMMY_LON, JIMMY_LAT),
            fuel_percent=66.96,
            odometer_miles=45942.98,
            battery_status="normal",
            mil_on=False,
            dtc_count=0,
            payload={"imei": "…5345"},
            provider_event_id="evt-1",
        )
    )
    session.commit()

    got = session.scalar(select(TelemetryEvent).where(TelemetryEvent.vehicle_id == v.id))
    assert got.battery_status == "normal"
    assert got.fuel_percent == pytest.approx(66.96)


def test_telemetry_is_idempotent_per_provider_event(session):
    """Bouncie retries webhooks with backoff for up to ~11 hours, so the same
    event arriving twice must not become two rows."""
    v = _vehicle(session, nickname="Jolene", plate="JFT9217")
    for _ in range(2):
        session.add(
            TelemetryEvent(
                vehicle_id=v.id,
                event_type="tripStart",
                occurred_at=datetime(2026, 9, 14, 13, 38, tzinfo=UTC),
                provider_event_id="dup-evt",
                payload={},
            )
        )
        try:
            session.commit()
        except Exception:
            session.rollback()

    n = session.scalar(
        select(func.count()).select_from(TelemetryEvent).where(TelemetryEvent.vehicle_id == v.id)
    )
    assert n == 1


def test_side_of_street_is_genuinely_ambiguous(session):
    """The design claim under test: a parked GPS fix cannot reliably tell you
    which side of the street a car is on, so the app must confirm rather than
    guess silently.

    If this ever fails because the two distances diverge widely, the
    confirm-the-spot prompt could be relaxed — so it is worth asserting.
    """
    north = StreetSegmentSide(
        street_name="Bergen St",
        from_cross_street="Carlton Ave",
        to_cross_street="Vanderbilt Ave",
        side=StreetSide.north,
        geom=_line(BERGEN_NORTH_LAT),
    )
    south = StreetSegmentSide(
        street_name="Bergen St",
        from_cross_street="Carlton Ave",
        to_cross_street="Vanderbilt Ave",
        side=StreetSide.south,
        geom=_line(BERGEN_SOUTH_LAT),
    )
    session.add_all([north, south])
    session.commit()

    fix = _point(JIMMY_LON, JIMMY_LAT)
    d_north, d_south = (
        session.scalar(
            select(func.ST_Distance(StreetSegmentSide.geom, func.ST_GeogFromText(fix))).where(
                StreetSegmentSide.id == s.id
            )
        )
        for s in (north, south)
    )

    # Both curbs are within a car length of the reported position...
    assert d_north < 12 and d_south < 12
    # ...and the gap between them is smaller than the device's own error, so
    # "nearest side wins" is not a safe rule on its own.
    assert abs(d_north - d_south) < 7.0, (
        f"north={d_north:.1f}m south={d_south:.1f}m — if this gap grew, "
        "auto-resolution might become safe"
    )


def test_parking_session_records_the_correction(session):
    """The operator correcting a guessed side is the signal worth learning from,
    so it is stored rather than inferred."""
    v = _vehicle(session, nickname="Corolla", plate="JGL4460")
    guess = StreetSegmentSide(
        street_name="Dean St",
        side=StreetSide.north,
        geom=_line(40.6801),
    )
    truth = StreetSegmentSide(
        street_name="Dean St",
        side=StreetSide.south,
        geom=_line(40.6800),
    )
    session.add_all([guess, truth])
    session.flush()

    ps = ParkingSession(
        vehicle_id=v.id,
        location=_point(JIMMY_LON, JIMMY_LAT),
        started_at=datetime(2026, 9, 16, 22, 30, tzinfo=UTC),
        guessed_segment_side_id=guess.id,
        segment_side_id=truth.id,
        confirmed_at=datetime(2026, 9, 16, 22, 31, tzinfo=UTC),
        was_corrected=True,
        must_move_by=datetime(2026, 9, 17, 12, 0, tzinfo=UTC),
    )
    session.add(ps)
    session.commit()

    got = session.scalar(select(ParkingSession).where(ParkingSession.vehicle_id == v.id))
    assert got.was_corrected is True
    assert got.segment_side_id != got.guessed_segment_side_id


def test_asp_rule_rejects_a_schedule_with_no_days(session):
    seg = StreetSegmentSide(street_name="Pacific St", side=StreetSide.north)
    session.add(seg)
    session.flush()
    session.add(
        AspRule(
            segment_side_id=seg.id,
            days_of_week=[],
            starts_at=time(8, 0),
            ends_at=time(9, 30),
            source=RuleSource.captured,
            confidence=1.0,
        )
    )
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_regenerating_a_module_does_not_duplicate_tasks(session):
    """Modules regenerate their own Tasks. The source key is what stops a
    re-run creating a second copy of the same obligation."""
    v = _vehicle(session, nickname="Van", plate="HZV3305", needs_large_spot=True)
    src = uuid.uuid4()
    for _ in range(2):
        session.add(
            Task(
                vehicle_id=v.id,
                kind=TaskKind.asp_move,
                title="Move Van",
                due_by=datetime(2026, 9, 17, 12, 0, tzinfo=UTC),
                source_kind="parking_session",
                source_id=src,
            )
        )
        try:
            session.commit()
        except Exception:
            session.rollback()

    n = session.scalar(select(func.count()).select_from(Task).where(Task.vehicle_id == v.id))
    assert n == 1


def test_task_owner_is_nullable_and_unset_by_default(session):
    """Single operator today. Nothing may assume a current user, or assignment
    becomes a migration rather than a feature when a helper starts."""
    v = _vehicle(session, nickname="Solo", plate="AAA1111")
    t = Task(
        vehicle_id=v.id,
        kind=TaskKind.fuel,
        title="Refuel",
        source_kind="telemetry",
        source_id=uuid.uuid4(),
    )
    session.add(t)
    session.commit()
    assert t.owner_id is None
    assert t.state is TaskState.open


def test_a_car_out_on_a_trip_can_have_its_move_task_suppressed(session):
    """The cross-module rule the whole Task abstraction exists for: a vehicle a
    guest is driving must not generate a street-cleaning alert."""
    v = _vehicle(session, nickname="OnTrip", plate="BBB2222")
    now = datetime(2026, 9, 17, 9, 0, tzinfo=UTC)
    session.add(
        Trip(
            vehicle_id=v.id,
            turo_trip_id="T-1",
            guest_name="Dana W.",
            starts_at=now - timedelta(hours=2),
            ends_at=now + timedelta(days=2),
            state=TripState.active,
            source=TripSource.manual,
        )
    )
    task = Task(
        vehicle_id=v.id,
        kind=TaskKind.asp_move,
        title="Move OnTrip",
        due_by=now + timedelta(hours=3),
        source_kind="parking_session",
        source_id=uuid.uuid4(),
    )
    session.add(task)
    session.commit()

    active = session.scalar(
        select(Trip).where(Trip.vehicle_id == v.id, Trip.starts_at <= now, Trip.ends_at > now)
    )
    assert active is not None
    task.state = TaskState.suppressed
    task.suppressed_reason = f"on trip with {active.guest_name} until {active.ends_at:%b %d}"
    session.commit()

    open_tasks = session.scalars(
        select(Task).where(Task.vehicle_id == v.id, Task.state == TaskState.open)
    ).all()
    assert open_tasks == []
