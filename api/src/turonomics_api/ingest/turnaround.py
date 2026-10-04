"""The work between trips.

A trip ending creates obligations: clean the car, put fuel in it, have it ready
before the next guest. Those are Tasks like any other, so they land on the same
run sheet as a street-cleaning deadline and compete for attention honestly.

Two decisions worth stating.

**The deadline is the next guest, not a fixed grace period.** A car booked again
tomorrow morning has to be ready by then; a car with nothing booked can wait.
Giving both the same due date would either manufacture urgency or hide it.

**Fuel is read, not asked.** Bouncie reports a tank level, so the app knows
whether a car needs gas rather than printing "check fuel" every time and
training the operator to tick it without looking. A vehicle whose device does
not report fuel gets no fuel task at all — an unanswerable question on the run
sheet is worse than a missing one.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from turonomics_api.db.models import (
    Task,
    TaskKind,
    TaskState,
    TelemetryEvent,
    Trip,
    TripState,
    Vehicle,
)

log = logging.getLogger("turonomics.ingest.turnaround")

SOURCE_KIND = "trip_turnaround"

# How long a car with nothing booked may sit before prep is nominally due. Not
# urgency — just a date, so the task is sortable rather than open-ended.
IDLE_GRACE = timedelta(hours=36)

# Below this, the tank needs attention before the next guest. A guess worth
# tuning: Turo guests return a car at roughly the level they got it, so this is
# about what the operator wants to hand over, not what the platform requires.
FUEL_THRESHOLD_PERCENT = 50.0

# A trip that ended long ago is history, not a chore. Without this, switching
# the module on would generate a task for every trip the fleet has ever run.
LOOKBACK = timedelta(days=4)


@dataclass
class TurnaroundResult:
    created: int = 0
    closed: int = 0

    def summary(self) -> str:
        return f"{self.created} turnaround task(s) created, {self.closed} closed"


def _latest_fuel(session: Session, vehicle_id: uuid.UUID) -> float | None:
    return session.scalar(
        select(TelemetryEvent.fuel_percent)
        .where(TelemetryEvent.vehicle_id == vehicle_id, TelemetryEvent.fuel_percent.isnot(None))
        .order_by(TelemetryEvent.occurred_at.desc())
        .limit(1)
    )


def _last_finished_trip(session: Session, vehicle_id: uuid.UUID, *, now: datetime) -> Trip | None:
    return session.scalar(
        select(Trip)
        .where(
            Trip.vehicle_id == vehicle_id,
            Trip.state != TripState.cancelled,
            Trip.ends_at <= now,
            Trip.ends_at >= now - LOOKBACK,
        )
        .order_by(Trip.ends_at.desc())
        .limit(1)
    )


def _next_trip(session: Session, vehicle_id: uuid.UUID, *, after: datetime) -> Trip | None:
    return session.scalar(
        select(Trip)
        .where(
            Trip.vehicle_id == vehicle_id,
            Trip.state != TripState.cancelled,
            Trip.starts_at > after,
        )
        .order_by(Trip.starts_at.asc())
        .limit(1)
    )


def _upsert(
    session: Session,
    *,
    vehicle: Vehicle,
    trip: Trip,
    kind: TaskKind,
    title: str,
    detail: str,
    due_by: datetime,
) -> bool:
    """Create the task if this trip has not already produced one of this kind.

    Keyed on the trip rather than the day, so the ten-minute poll updates one
    task instead of adding a sixth copy of "clean the Transit".
    """
    existing = session.scalar(
        select(Task).where(
            Task.source_kind == SOURCE_KIND,
            Task.source_id == trip.id,
            Task.kind == kind,
        )
    )
    if existing is not None:
        # Don't reopen something the operator has already dealt with.
        if existing.state is TaskState.open:
            existing.due_by = due_by
            existing.detail = detail
            session.flush()
        return False

    session.add(
        Task(
            vehicle_id=vehicle.id,
            kind=kind,
            title=title,
            detail=detail,
            due_by=due_by,
            source_kind=SOURCE_KIND,
            source_id=trip.id,
        )
    )
    session.flush()
    return True


def refresh_turnaround_tasks(
    session: Session, *, vehicle: Vehicle, now: datetime | None = None
) -> TurnaroundResult:
    """Create or retire this vehicle's between-trips work."""
    now = now or datetime.now(UTC)
    result = TurnaroundResult()

    trip = _last_finished_trip(session, vehicle.id, now=now)
    if trip is None:
        return result

    # Out with a guest again: whatever prep was outstanding, the window for it
    # has closed. Cancelled rather than left open, because an overdue task for
    # a car that is already rented is noise the operator cannot act on — but
    # cancelled with a reason, so a missed turnaround is visible rather than
    # quietly swept away.
    active = session.scalar(
        select(Trip).where(
            Trip.vehicle_id == vehicle.id,
            Trip.state != TripState.cancelled,
            Trip.starts_at <= now,
            Trip.ends_at > now,
        )
    )
    if active is not None:
        stale = session.scalars(
            select(Task).where(
                Task.source_kind == SOURCE_KIND,
                Task.vehicle_id == vehicle.id,
                Task.state == TaskState.open,
            )
        ).all()
        for task in stale:
            task.state = TaskState.cancelled
            task.suppressed_reason = f"the next trip started{_guest_suffix(active)}"
            result.closed += 1
        if stale:
            session.flush()
        return result

    following = _next_trip(session, vehicle.id, after=now)
    due_by = following.starts_at if following else trip.ends_at + IDLE_GRACE
    when = (
        f"before {following.guest_name or 'the next guest'} collects it"
        if following
        else "no trip booked yet"
    )

    if _upsert(
        session,
        vehicle=vehicle,
        trip=trip,
        kind=TaskKind.turnaround,
        title=f"Prep {vehicle.nickname}",
        detail=f"clean and check over after {trip.guest_name or 'the last trip'} — {when}",
        due_by=due_by,
    ):
        result.created += 1

    # Fuel only when the car can actually answer the question.
    fuel = _latest_fuel(session, vehicle.id) if vehicle.reports_fuel_level else None
    needs_fuel = fuel is not None and fuel < FUEL_THRESHOLD_PERCENT
    if needs_fuel:
        if _upsert(
            session,
            vehicle=vehicle,
            trip=trip,
            kind=TaskKind.fuel,
            title=f"Fuel {vehicle.nickname}",
            detail=f"tank at {fuel:.0f}% — {when}",
            due_by=due_by,
        ):
            result.created += 1
    elif fuel is not None:
        # The tank came back up, so somebody filled it. Marked done rather
        # than left standing: this task said "tank at 26%" beside a gauge
        # reading 93% for an hour, because creating it was conditional and
        # retiring it was not. Done rather than cancelled, because the
        # obligation was discharged — the fuel went in — not withdrawn.
        for task in session.scalars(
            select(Task).where(
                Task.source_kind == SOURCE_KIND,
                Task.source_id == trip.id,
                Task.kind == TaskKind.fuel,
                Task.state == TaskState.open,
            )
        ).all():
            task.state = TaskState.done
            task.completed_at = now
            task.detail = f"tank back up to {fuel:.0f}%"
            result.closed += 1
            session.flush()

    if result.created or result.closed:
        log.info("%s: %s", vehicle.nickname, result.summary())
    return result


def _guest_suffix(trip: Trip) -> str:
    return f" with {trip.guest_name}" if trip.guest_name else ""
