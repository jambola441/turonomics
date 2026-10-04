"""The work between trips.

Two claims under test. The deadline is the next guest rather than a fixed grace
period, because that is what makes the task sortable against a street-cleaning
deadline instead of competing with invented urgency. And fuel is read from the
car rather than asked about, because a prompt that appears every single time
gets ticked without being read.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from turonomics_api.db.models import (
    Task,
    TaskKind,
    TaskState,
    TelemetryEvent,
    Trip,
    TripSource,
    TripState,
    Vehicle,
)
from turonomics_api.ingest.turnaround import (
    FUEL_THRESHOLD_PERCENT,
    IDLE_GRACE,
    LOOKBACK,
    refresh_turnaround_tasks,
)

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL") and not os.environ.get("DATABASE_URL"),
    reason="no database configured",
)

NOW = datetime(2026, 10, 8, 18, 0, tzinfo=UTC)


def _van(session, *, reports_fuel: bool = True) -> Vehicle:
    van = Vehicle(
        nickname="Bubba", make="Ford", model="Transit", year=2024, reports_fuel_level=reports_fuel
    )
    session.add(van)
    session.flush()
    return van


def _trip(session, van, *, starts, ends, guest="Dana", state=TripState.completed) -> Trip:
    trip = Trip(
        vehicle_id=van.id,
        turo_trip_id=f"R{int(starts.timestamp())}",
        guest_name=guest,
        starts_at=starts,
        ends_at=ends,
        state=state,
        source=TripSource.email,
    )
    session.add(trip)
    session.flush()
    return trip


def _fuel(session, van, percent: float, *, at=NOW) -> None:
    session.add(
        TelemetryEvent(
            vehicle_id=van.id,
            event_type="statsSnapshot",
            occurred_at=at,
            fuel_percent=percent,
            payload={},
            provider_event_id=f"f{at.isoformat()}{percent}",
        )
    )
    session.flush()


def test_a_finished_trip_creates_prep_work(session):
    van = _van(session)
    _trip(session, van, starts=NOW - timedelta(days=3), ends=NOW - timedelta(hours=2))
    _fuel(session, van, 80.0)

    result = refresh_turnaround_tasks(session, vehicle=van, now=NOW)
    assert result.created == 1, "prep, but no fuel task on a full tank"

    task = session.scalar(select(Task).where(Task.kind == TaskKind.turnaround))
    assert task.title == "Prep Bubba"
    assert "Dana" in task.detail
    assert task.due_by == NOW - timedelta(hours=2) + IDLE_GRACE


def test_the_deadline_is_the_next_guest_when_one_is_booked(session):
    """A car booked again tomorrow morning has to be ready by then. Using the
    idle grace period for both would either invent urgency or hide it."""
    van = _van(session)
    _trip(session, van, starts=NOW - timedelta(days=3), ends=NOW - timedelta(hours=2))
    collects_at = NOW + timedelta(hours=14)
    _trip(session, van, starts=collects_at, ends=collects_at + timedelta(days=2),
          guest="Marcus", state=TripState.upcoming)
    _fuel(session, van, 80.0)

    refresh_turnaround_tasks(session, vehicle=van, now=NOW)
    task = session.scalar(select(Task).where(Task.kind == TaskKind.turnaround))
    assert task.due_by == collects_at
    assert "Marcus" in task.detail


def test_a_low_tank_produces_a_fuel_task_with_the_actual_level(session):
    van = _van(session)
    _trip(session, van, starts=NOW - timedelta(days=2), ends=NOW - timedelta(hours=1))
    _fuel(session, van, 18.0)

    assert refresh_turnaround_tasks(session, vehicle=van, now=NOW).created == 2
    fuel_task = session.scalar(select(Task).where(Task.kind == TaskKind.fuel))
    assert "18%" in fuel_task.detail


def test_a_car_that_cannot_report_fuel_is_not_asked_about_it(session):
    """An unanswerable question on the run sheet is worse than a missing one."""
    van = _van(session, reports_fuel=False)
    _trip(session, van, starts=NOW - timedelta(days=2), ends=NOW - timedelta(hours=1))
    _fuel(session, van, 10.0)

    refresh_turnaround_tasks(session, vehicle=van, now=NOW)
    assert session.scalar(
        select(func.count()).select_from(Task).where(Task.kind == TaskKind.fuel)
    ) == 0


def test_a_tank_above_the_threshold_produces_nothing(session):
    van = _van(session)
    _trip(session, van, starts=NOW - timedelta(days=2), ends=NOW - timedelta(hours=1))
    _fuel(session, van, FUEL_THRESHOLD_PERCENT + 1)
    refresh_turnaround_tasks(session, vehicle=van, now=NOW)
    assert session.scalar(
        select(func.count()).select_from(Task).where(Task.kind == TaskKind.fuel)
    ) == 0


def test_the_poll_does_not_pile_up_copies(session):
    """It runs every ten minutes. Six copies of "clean the Transit" an hour
    would make the run sheet useless within a day."""
    van = _van(session)
    _trip(session, van, starts=NOW - timedelta(days=2), ends=NOW - timedelta(hours=1))
    _fuel(session, van, 20.0)
    for _ in range(6):
        refresh_turnaround_tasks(session, vehicle=van, now=NOW)
    assert session.scalar(select(func.count()).select_from(Task)) == 2


def test_work_already_done_is_not_reopened(session):
    van = _van(session)
    _trip(session, van, starts=NOW - timedelta(days=2), ends=NOW - timedelta(hours=1))
    _fuel(session, van, 20.0)
    refresh_turnaround_tasks(session, vehicle=van, now=NOW)

    for task in session.scalars(select(Task)).all():
        task.state = TaskState.done
    session.flush()

    refresh_turnaround_tasks(session, vehicle=van, now=NOW + timedelta(minutes=10))
    assert session.scalar(
        select(func.count()).select_from(Task).where(Task.state == TaskState.open)
    ) == 0


def test_prep_is_cancelled_with_a_reason_once_the_next_trip_starts(session):
    """The window closed. An overdue task for a car that is already rented is
    noise the operator cannot act on — but cancelling silently would hide a
    turnaround that genuinely got missed."""
    van = _van(session)
    _trip(session, van, starts=NOW - timedelta(days=3), ends=NOW - timedelta(hours=2))
    _fuel(session, van, 20.0)
    refresh_turnaround_tasks(session, vehicle=van, now=NOW)

    later = NOW + timedelta(hours=4)
    _trip(session, van, starts=later - timedelta(hours=1), ends=later + timedelta(days=2),
          guest="Marcus", state=TripState.active)

    result = refresh_turnaround_tasks(session, vehicle=van, now=later)
    assert result.closed == 2
    task = session.scalar(select(Task).where(Task.kind == TaskKind.turnaround))
    assert task.state is TaskState.cancelled
    assert "Marcus" in (task.suppressed_reason or ""), task.suppressed_reason


def test_an_old_trip_does_not_generate_work_now(session):
    """Switching the module on must not produce a task for every trip the
    fleet has ever run."""
    van = _van(session)
    _trip(session, van, starts=NOW - LOOKBACK - timedelta(days=20),
          ends=NOW - LOOKBACK - timedelta(days=18))
    _fuel(session, van, 10.0)
    assert refresh_turnaround_tasks(session, vehicle=van, now=NOW).created == 0
    assert session.scalar(select(func.count()).select_from(Task)) == 0


def test_a_cancelled_trip_creates_no_prep_work(session):
    """Nobody drove it, so there is nothing to clean."""
    van = _van(session)
    _trip(session, van, starts=NOW - timedelta(days=2), ends=NOW - timedelta(hours=1),
          state=TripState.cancelled)
    _fuel(session, van, 10.0)
    assert refresh_turnaround_tasks(session, vehicle=van, now=NOW).created == 0


def test_a_car_still_out_with_a_guest_gets_no_prep_work_yet(session):
    van = _van(session)
    _trip(session, van, starts=NOW - timedelta(days=1), ends=NOW + timedelta(days=2),
          state=TripState.active)
    _fuel(session, van, 10.0)
    assert refresh_turnaround_tasks(session, vehicle=van, now=NOW).created == 0


# ---------------------------------------------------------------------------
# A fuel task that outlives the empty tank
# ---------------------------------------------------------------------------


def test_filling_the_tank_retires_the_fuel_task(session):
    """Found on the live fleet the hour the run sheet first showed tasks.

    A car came back from a trip with a quarter tank, the task was created,
    somebody filled her, and the task sat there reading "tank at 26%" beside a
    gauge showing 93%. Creating it was conditional on the fuel level; retiring
    it was not conditional on anything, because nothing retired it at all.
    """
    van = _van(session)
    _trip(session, van, starts=NOW - timedelta(days=2), ends=NOW - timedelta(hours=1))
    _fuel(session, van, 26.0, at=NOW - timedelta(minutes=30))

    refresh_turnaround_tasks(session, vehicle=van, now=NOW)
    session.flush()
    fuel_task = session.scalar(select(Task).where(Task.kind == TaskKind.fuel))
    assert fuel_task.state is TaskState.open
    assert "26%" in fuel_task.detail

    _fuel(session, van, 93.0, at=NOW - timedelta(minutes=5))
    result = refresh_turnaround_tasks(session, vehicle=van, now=NOW)
    session.flush()

    session.refresh(fuel_task)
    assert fuel_task.state is TaskState.done, "the obligation was discharged"
    assert fuel_task.completed_at is not None
    assert "93%" in fuel_task.detail, "and the detail stops lying about the level"
    assert result.closed >= 1


def test_a_tank_still_low_keeps_its_task_open(session):
    """The other half. Retiring on every poll would make the task useless."""
    van = _van(session)
    _trip(session, van, starts=NOW - timedelta(days=2), ends=NOW - timedelta(hours=1))
    _fuel(session, van, 26.0, at=NOW - timedelta(minutes=30))
    refresh_turnaround_tasks(session, vehicle=van, now=NOW)
    session.flush()

    _fuel(session, van, 31.0, at=NOW - timedelta(minutes=5))
    refresh_turnaround_tasks(session, vehicle=van, now=NOW)
    session.flush()
    assert session.scalar(select(Task).where(Task.kind == TaskKind.fuel)).state is TaskState.open


def test_a_full_tank_on_a_car_that_cannot_report_closes_nothing(session):
    """The retire path must not invent a task to close on a car whose fuel
    level is an unanswerable question."""
    van = _van(session, reports_fuel=False)
    _trip(session, van, starts=NOW - timedelta(days=2), ends=NOW - timedelta(hours=1))
    _fuel(session, van, 93.0, at=NOW - timedelta(minutes=5))
    result = refresh_turnaround_tasks(session, vehicle=van, now=NOW)
    session.flush()
    assert session.scalars(select(Task).where(Task.kind == TaskKind.fuel)).all() == []
    assert result.closed == 0


def test_a_car_that_stops_reporting_fuel_does_not_crash_the_poll(session):
    """The reachable edge the ``fuel is not None`` guard exists for.

    ``reports_fuel_level`` is a column, not a constant — a device swap or a
    capability refresh can turn it off on a car that already has an open fuel
    task. Without the guard the retire path formats ``None`` as a percentage
    and takes the whole poll down with it, which is a steep price for tidying
    up a task nobody can answer any more.
    """
    van = _van(session, reports_fuel=True)
    _trip(session, van, starts=NOW - timedelta(days=2), ends=NOW - timedelta(hours=1))
    _fuel(session, van, 26.0, at=NOW - timedelta(minutes=30))
    refresh_turnaround_tasks(session, vehicle=van, now=NOW)
    session.flush()
    task = session.scalar(select(Task).where(Task.kind == TaskKind.fuel))
    assert task.state is TaskState.open

    van.reports_fuel_level = False
    session.flush()

    refresh_turnaround_tasks(session, vehicle=van, now=NOW)  # must not raise
    session.flush()
    session.refresh(task)
    assert task.state is TaskState.open, "left alone rather than closed on a guess"
    assert "26%" in task.detail, "and not rewritten with a level nobody reported"
