"""Which deadlines are worth interrupting someone for, and when.

The rules are separated from delivery precisely so they can be asserted like
this: a deadline is a time, "should you be told now" is a function of it, and
getting that wrong is how an alerting system trains its only user to ignore it.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from turonomics_api.db.models import Task, TaskKind, TaskState, Vehicle
from turonomics_api.notify.alerts import OVERDUE_WINDOW, alert_for

LEADS = [720, 60]
DUE = datetime(2026, 10, 9, 13, 0, tzinfo=UTC)  # 9am in New York


def _task(**kwargs) -> Task:
    return Task(
        id=kwargs.pop("id", uuid.uuid4()),
        kind=TaskKind.asp_move,
        state=TaskState.open,
        title=kwargs.pop("title", "Move for street cleaning"),
        due_by=kwargs.pop("due_by", DUE),
        location_label=kwargs.pop("location_label", "Bergen St — north side"),
        **kwargs,
    )


def _vehicle(nickname: str = "Jimmy") -> Vehicle:
    return Vehicle(id=uuid.uuid4(), nickname=nickname, make="Toyota", model="4Runner", year=2023)


def _at(hours_before: float):
    return alert_for(_task(), _vehicle(), now=DUE - timedelta(hours=hours_before), leads=LEADS)


def _stage(alert) -> str:
    return alert.dedupe_key.rsplit(":", 1)[1]


@pytest.mark.parametrize(
    ("hours_before", "stage"),
    [(14, None), (12, "lead720"), (5, "lead720"), (1.5, "lead720"), (0.5, "lead60")],
)
def test_each_window_opens_when_it_should(hours_before, stage):
    alert = _at(hours_before)
    assert (alert and _stage(alert)) == stage


def test_the_shortest_eligible_window_wins_not_the_longest():
    """Half an hour out, both leads are eligible. Firing the longest would say
    "in 12 hours" about something due in thirty minutes.

    This is also what stops a service that slept through both windows — a
    deploy, a weekend, a free instance spinning down — from sending every
    missed warning in the same second.
    """
    assert _stage(_at(0.5)) == "lead60"


def test_a_passed_deadline_alerts_once_and_then_stops_being_news():
    assert _stage(_at(-0.2)) == "overdue"
    stale = OVERDUE_WINDOW.total_seconds() / 3600 + 1
    assert _at(-stale) is None


def test_the_deadline_is_part_of_the_key_not_just_the_task():
    """A move task is reused for the life of a parking session and its
    ``due_by`` rolls forward to the next cleaning window once one passes. Keyed
    on the task alone, the next week's warning is suppressed by this week's
    having already gone out — the car then quietly stops being alerted on.
    """
    shared = uuid.uuid4()
    this_week = alert_for(
        _task(id=shared), _vehicle(), now=DUE - timedelta(hours=12), leads=LEADS
    )
    later = DUE + timedelta(days=7)
    next_week = alert_for(
        _task(id=shared, due_by=later), _vehicle(), now=later - timedelta(hours=12), leads=LEADS
    )
    assert this_week.dedupe_key != next_week.dedupe_key


def test_the_same_deadline_at_the_same_stage_is_the_same_alert():
    """The poll runs every ten minutes; the key has to be stable between them
    or deduplication does nothing."""
    task, vehicle = _task(), _vehicle()
    first = alert_for(task, vehicle, now=DUE - timedelta(hours=5), leads=LEADS)
    second = alert_for(task, vehicle, now=DUE - timedelta(hours=4, minutes=50), leads=LEADS)
    assert first.dedupe_key == second.dedupe_key


def test_the_car_comes_first_because_it_is_read_on_a_lock_screen():
    alert = _at(12)
    assert alert.title.startswith("Jimmy:")


def test_the_body_says_the_local_time_and_how_long_there_is():
    alert = _at(12)
    assert "9:00 am" in alert.body, "street cleaning is a local-time rule"
    assert "in 12h00" in alert.body
    assert "Bergen St — north side" in alert.body


def test_an_overdue_body_does_not_claim_the_deadline_is_ahead():
    alert = _at(-0.2)
    assert "Was due" in alert.body and "ago" in alert.body


def test_only_the_last_warning_and_the_overdue_one_wake_a_phone():
    """Urgency is a cost. Spending it on the twelve-hour heads-up is how a
    phone's owner turns the whole thing off."""
    assert _at(12).urgent is False
    assert _at(0.5).urgent is True
    assert _at(-0.2).urgent is True


def test_the_link_names_the_car_so_tapping_it_lands_somewhere_useful():
    vehicle = _vehicle()
    alert = alert_for(_task(), vehicle, now=DUE - timedelta(hours=12), leads=LEADS)
    assert f"car={vehicle.id}" in alert.url


def test_a_task_with_no_deadline_is_never_an_alert():
    assert alert_for(_task(due_by=None), _vehicle(), now=DUE, leads=LEADS) is None
