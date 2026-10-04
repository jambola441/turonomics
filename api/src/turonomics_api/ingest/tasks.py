"""Turn parking state into the obligations the run sheet shows.

Every module emits ``Task`` and the run sheet consumes nothing else. This is
the module where that pays for itself: a car out on a guest trip must not
generate a street-cleaning alert, and that is one rule here rather than four
modules aware of each other.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from turonomics_api.asp.schedule import Rule, next_cleaning_window
from turonomics_api.db.models import (
    AspRule,
    AspSuspension,
    ParkingSession,
    Task,
    TaskKind,
    TaskState,
    Trip,
    TripState,
    Vehicle,
)
from turonomics_api.settings import fleet_timezone

SOURCE_KIND = "parking_session"


def suspended_dates(session: Session) -> frozenset[date]:
    return frozenset(session.scalars(select(AspSuspension.suspended_on)).all())


def rules_for(session: Session, segment_side_id: uuid.UUID) -> list[Rule]:
    rows = session.scalars(select(AspRule).where(AspRule.segment_side_id == segment_side_id)).all()
    return [
        Rule(
            days_of_week=tuple(r.days_of_week),
            starts_at=r.starts_at,
            ends_at=r.ends_at,
        )
        for r in rows
    ]


def active_trip(session: Session, vehicle_id: uuid.UUID, *, now: datetime) -> Trip | None:
    """The trip a guest is currently on, if any."""
    return session.scalar(
        select(Trip)
        .where(
            Trip.vehicle_id == vehicle_id,
            Trip.starts_at <= now,
            Trip.ends_at > now,
            Trip.state.in_((TripState.active, TripState.upcoming)),
        )
        .order_by(Trip.starts_at.desc())
        .limit(1)
    )


def _retire_stale_move_tasks(session: Session, *, vehicle: Vehicle, keep: uuid.UUID | None) -> None:
    """Cancel this vehicle's open move tasks except the one for ``keep``.

    ``keep`` is the currently open parking session, if there is one. Everything
    else is an obligation for a spot the car has left.
    """
    query = select(Task).where(
        Task.vehicle_id == vehicle.id,
        Task.kind == TaskKind.asp_move,
        Task.state == TaskState.open,
    )
    if keep is not None:
        query = query.where(Task.source_id != keep)
    stale = session.scalars(query).all()
    for task in stale:
        task.state = TaskState.cancelled
        task.suppressed_reason = "the car left this spot"
    if stale:
        session.flush()


def refresh_move_task(
    session: Session,
    *,
    vehicle: Vehicle,
    now: datetime | None = None,
) -> Task | None:
    """Create, update or suppress this vehicle's street-cleaning obligation.

    Returns the task, or ``None`` when there is nothing to do — no open parking
    session, no confirmed side, or no rules for that side. Note "no confirmed
    side" produces no deadline on purpose: an unconfirmed guess is a coin flip
    (see ``resolve_side``), and a confidently wrong deadline is worse than an
    admitted gap. Prompting for that confirmation is a separate obligation, not
    this one.
    """
    now = now or datetime.now(UTC)

    parking = session.scalar(
        select(ParkingSession)
        .where(ParkingSession.vehicle_id == vehicle.id, ParkingSession.ended_at.is_(None))
        .order_by(ParkingSession.started_at.desc())
        .limit(1)
    )
    # Retire anything left over from a spot the car has left, before any of the
    # early returns below. A move task is scoped to the parking session that
    # produced it, so a car that parks somewhere new gets a new session and the
    # old task is never looked at again — it just stays open, telling the
    # operator to go and move a car that is no longer there. The run sheet is
    # only worth trusting if everything on it is still true, and one phantom
    # task teaches the operator to ignore the real ones.
    _retire_stale_move_tasks(
        session, vehicle=vehicle, keep=parking.id if parking is not None else None
    )
    if parking is None:
        return None

    existing = session.scalar(
        select(Task).where(
            Task.source_kind == SOURCE_KIND,
            Task.source_id == parking.id,
            Task.kind == TaskKind.asp_move,
        )
    )

    if parking.segment_side_id is None:
        return existing

    rules = rules_for(session, parking.segment_side_id)
    window = next_cleaning_window(
        rules, now=now, suspended_dates=suspended_dates(session), tz=fleet_timezone()
    )
    if window is None:
        return existing

    parking.must_move_by = window.starts_at

    side = parking.segment_side
    where = f"{side.street_name} — {side.side.value} side" if side else "unknown block"

    if existing is None:
        existing = Task(
            vehicle_id=vehicle.id,
            kind=TaskKind.asp_move,
            title=f"Move {vehicle.nickname}",
            source_kind=SOURCE_KIND,
            source_id=parking.id,
        )
        session.add(existing)

    # A deadline that moved is a new obligation, even on the same row. This
    # task is scoped to the parking session, but its due_by rolls forward to
    # the next cleaning window once one passes — so a task ticked off for
    # Monday would otherwise stay done through Thursday's sweep, and that car
    # would silently never be warned about again while sitting in one spot.
    if existing.state is TaskState.done and existing.due_by != window.starts_at:
        existing.state = TaskState.open
        existing.completed_at = None
        existing.completed_by_id = None

    existing.due_by = window.starts_at
    existing.location = parking.location
    existing.location_label = where
    existing.detail = (
        f"Street cleaning {window.starts_at:%-I:%M%p}–{window.ends_at:%-I:%M%p}".lower().replace(
            ":00", ""
        )
    )

    # The cross-module rule. A car a guest is driving is not parked, is not the
    # operator's to move, and must not appear on the run sheet.
    trip = active_trip(session, vehicle.id, now=now)
    if trip is not None:
        existing.state = TaskState.suppressed
        guest = trip.guest_name or "a guest"
        existing.suppressed_reason = f"on trip with {guest} until {trip.ends_at:%b %-d}"
    elif existing.state is TaskState.suppressed:
        # The trip ended; the obligation comes back rather than staying hidden.
        existing.state = TaskState.open
        existing.suppressed_reason = None

    session.flush()
    return existing


def refresh_all_move_tasks(session: Session, *, now: datetime | None = None) -> list[Task]:
    now = now or datetime.now(UTC)
    tasks = []
    for vehicle in session.scalars(select(Vehicle).where(Vehicle.is_active.is_(True))):
        task = refresh_move_task(session, vehicle=vehicle, now=now)
        if task is not None:
            tasks.append(task)
    session.commit()
    return tasks
