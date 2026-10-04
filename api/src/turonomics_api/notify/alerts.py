"""Decide which open tasks deserve a notification right now.

Separate from delivery on purpose. The question "should the operator be told
about this, and in these words" is a fleet question with a right answer that
can be asserted in a test; the question "did the push service accept it" is
plumbing. Keeping them apart also means the rules can be run and logged with no
channel configured at all, which is how the timing gets checked against a real
fleet before any key exists.

A task's deadline stays true for hours, so an alert is pinned to a *stage* of
its approach rather than to the condition. Each stage fires once.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

from sqlalchemy import select
from sqlalchemy.orm import Session

from turonomics_api.db.models import Task, TaskState, Vehicle
from turonomics_api.settings import asp_alert_lead_minutes, fleet_timezone, site_url

# A deadline that passed a week ago is history, not an alert. The window only
# needs to be long enough that a deploy over a weekend still reports the
# Monday-morning move it slept through.
OVERDUE_WINDOW = timedelta(days=2)

OVERDUE_STAGE = "overdue"


@dataclass(frozen=True)
class Alert:
    """One thing to tell the operator, once."""

    dedupe_key: str
    title: str
    body: str
    url: str
    task_id: uuid.UUID | None = None
    # Push services treat this as a hint about waking the device. A move
    # deadline is worth waking a phone for; nothing else here is.
    urgent: bool = False


def _stage(task: Task, *, now: datetime, leads: list[int]) -> str | None:
    """Which stage of this task's approach ``now`` falls in, if any.

    The *shortest* eligible lead, not the longest. When the service has been
    asleep through several windows — a free Render instance, a deploy, a
    weekend — every one of them is eligible at once, and firing them all sends
    four notifications in the same second saying progressively less true
    things. The shortest is the only one that is still accurate.
    """
    if task.due_by is None:
        return None
    due = task.due_by if task.due_by.tzinfo else task.due_by.replace(tzinfo=UTC)
    if now >= due:
        return OVERDUE_STAGE if now - due <= OVERDUE_WINDOW else None
    for lead in sorted(leads):
        if now >= due - timedelta(minutes=lead):
            return f"lead{lead}"
    return None


def _phrase(due: datetime, *, now: datetime) -> str:
    """How long there is, in words, rounded the way a person would say it."""
    if now >= due:
        minutes = int((now - due).total_seconds() // 60)
        if minutes < 60:
            return f"{minutes} min ago" if minutes else "just now"
        return f"{minutes // 60}h{minutes % 60:02d} ago"
    remaining = int((due - now).total_seconds() // 60)
    if remaining < 60:
        return f"in {remaining} min"
    if remaining < 24 * 60:
        return f"in {remaining // 60}h{remaining % 60:02d}"
    return f"in {remaining // (24 * 60)}d"


def _url(vehicle_id: uuid.UUID) -> str:
    base = site_url()
    joiner = "&" if "?" in base else "?"
    return f"{base}{joiner}{urlencode({'car': str(vehicle_id)})}"


def alert_for(task: Task, vehicle: Vehicle, *, now: datetime, leads: list[int]) -> Alert | None:
    """The alert this task warrants now, or ``None``."""
    stage = _stage(task, now=now, leads=leads)
    if stage is None:
        return None
    due = task.due_by
    assert due is not None  # _stage returns None without one
    if due.tzinfo is None:
        due = due.replace(tzinfo=UTC)
    local = due.astimezone(fleet_timezone())
    when = local.strftime("%-I:%M %p").lower()
    overdue = stage == OVERDUE_STAGE
    # Waking a phone is a cost. The last warning before a deadline and the
    # notice that it has passed earn it; the twelve-hour heads-up does not.
    urgent = overdue or (bool(leads) and stage == f"lead{min(leads)}")
    # The car's name leads, because the operator is reading this on a lock
    # screen and "Jimmy" is the word that says whether it is their problem.
    title = f"{vehicle.nickname}: {task.title}"
    pieces = [f"{'Was due' if overdue else 'By'} {when} ({_phrase(due, now=now)})"]
    if task.location_label:
        pieces.append(task.location_label)
    if task.detail:
        pieces.append(task.detail)
    return Alert(
        # The deadline is part of the key, not just the task and the stage. A
        # move task is reused for the life of a parking session and its
        # ``due_by`` rolls forward to the next cleaning window once one passes
        # — same row, new obligation. Keyed on the task alone, Monday's warning
        # would be suppressed by Thursday's having already gone out.
        dedupe_key=f"task:{task.id}:{int(due.timestamp())}:{stage}",
        title=title,
        body=" — ".join(pieces),
        url=_url(vehicle.id),
        task_id=task.id,
        urgent=urgent,
    )


def alerts_due(session: Session, *, now: datetime | None = None) -> list[Alert]:
    """Every alert the fleet warrants at this moment, before deduplication.

    Deduplication is the dispatcher's job: this says what is true, and the
    dispatcher remembers what has already been said.
    """
    now = now or datetime.now(UTC)
    leads = asp_alert_lead_minutes()
    horizon = now + timedelta(minutes=max(leads)) if leads else now
    rows = session.execute(
        select(Task, Vehicle)
        .join(Vehicle, Vehicle.id == Task.vehicle_id)
        .where(
            Task.state == TaskState.open,
            Task.due_by.is_not(None),
            Task.due_by <= horizon,
            Task.due_by >= now - OVERDUE_WINDOW,
        )
        .order_by(Task.due_by)
    ).all()
    found = [alert_for(task, vehicle, now=now, leads=leads) for task, vehicle in rows]
    return [alert for alert in found if alert is not None]
