"""The run sheet showing what has to happen, and letting it be ticked off.

``models.py`` says Task is the one abstraction the run sheet consumes and that
every module emits them. Until this, the whole output of that abstraction was
an integer the UI did not render: a car could have three things to do and no
way to find out what they were.

The interesting behaviour is not the list. It is what "done" means on a task
whose deadline outlives it.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, time, timedelta

import pytest
from sqlalchemy import select

from turonomics_api.db.models import (
    AspRule,
    RuleSource,
    StreetSegmentSide,
    StreetSide,
    Task,
    TaskKind,
    TaskState,
    Vehicle,
)
from turonomics_api.ingest.parking import confirm_side, open_parking_session
from turonomics_api.ingest.tasks import refresh_move_task

from .conftest import requires_db

pytestmark = requires_db

NOW = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)
LAT, LON = 40.679884, -73.970193


@pytest.fixture()
def car(session):
    vehicle = Vehicle(nickname="Jolene", make="Toyota", model="Corolla", year=2025)
    session.add(vehicle)
    session.flush()
    return vehicle


def _task(session, car, **kwargs) -> Task:
    task = Task(
        vehicle_id=car.id,
        kind=kwargs.pop("kind", TaskKind.turnaround),
        state=kwargs.pop("state", TaskState.open),
        title=kwargs.pop("title", "Top up the tank"),
        **kwargs,
    )
    session.add(task)
    session.commit()
    return task


# ---------------------------------------------------------------------------
# The list
# ---------------------------------------------------------------------------


def test_the_fleet_view_says_what_the_work_actually_is(session, car, api_client):
    _task(session, car, title="Top up the tank", detail="38% left")
    tasks = api_client.get("/api/fleet").json()["vehicles"][0]["tasks"]
    assert [t["title"] for t in tasks] == ["Top up the tank"]
    assert tasks[0]["detail"] == "38% left"


def test_the_soonest_deadline_comes_first_and_undated_work_goes_last(session, car, api_client):
    _task(session, car, title="no deadline")
    _task(session, car, title="later", due_by=NOW + timedelta(days=2), kind=TaskKind.fuel)
    _task(session, car, title="sooner", due_by=NOW + timedelta(hours=2), kind=TaskKind.asp_move)
    titles = [t["title"] for t in api_client.get("/api/fleet").json()["vehicles"][0]["tasks"]]
    assert titles == ["sooner", "later", "no deadline"]


@pytest.mark.parametrize("hidden", [TaskState.done, TaskState.cancelled, TaskState.suppressed])
def test_only_open_work_is_on_the_sheet(session, car, api_client, hidden):
    """A suppressed task is a car a guest is driving — not the operator's to do.
    Showing it anyway is how a run sheet stops being a list of true things."""
    _task(session, car, state=hidden)
    assert api_client.get("/api/fleet").json()["vehicles"][0]["tasks"] == []


def test_the_count_and_the_list_cannot_disagree(session, car, api_client):
    _task(session, car, title="a")
    _task(session, car, title="b", kind=TaskKind.fuel)
    _task(session, car, title="done one", state=TaskState.done, kind=TaskKind.maintenance)
    vehicle = api_client.get("/api/fleet").json()["vehicles"][0]
    assert vehicle["open_task_count"] == len(vehicle["tasks"]) == 2


# ---------------------------------------------------------------------------
# Ticking it off
# ---------------------------------------------------------------------------


def test_done_takes_it_off_the_sheet_and_returns_the_whole_car(session, car, api_client):
    """The response is the vehicle, so the card re-renders from one round trip.
    A tick that shows nothing for a minute reads as a tick that failed."""
    task = _task(session, car)
    body = api_client.post(f"/api/tasks/{task.id}/done").json()
    assert body["id"] == str(car.id)
    assert body["tasks"] == []
    assert body["open_task_count"] == 0
    session.expire_all()
    assert session.get(Task, task.id).completed_at is not None


def test_ticking_twice_is_success_not_a_conflict(session, car, api_client):
    """The button is on a phone and the network is the network."""
    task = _task(session, car)
    assert api_client.post(f"/api/tasks/{task.id}/done").status_code == 200
    assert api_client.post(f"/api/tasks/{task.id}/done").status_code == 200


def test_undo_puts_it_back(session, car, api_client):
    task = _task(session, car)
    api_client.post(f"/api/tasks/{task.id}/done")
    body = api_client.post(f"/api/tasks/{task.id}/undo").json()
    assert [t["title"] for t in body["tasks"]] == ["Top up the tank"]
    session.expire_all()
    assert session.get(Task, task.id).completed_at is None


def test_undo_will_not_resurrect_something_a_module_retired(session, car, api_client):
    """Cancelled means it stopped being true — a move task for a spot the car
    has left. Putting that back would be a lie on the run sheet."""
    task = _task(session, car, state=TaskState.cancelled)
    assert api_client.post(f"/api/tasks/{task.id}/undo").status_code == 409


def test_an_unknown_task_is_a_404_not_a_500(api_client):
    assert api_client.post(f"/api/tasks/{uuid.uuid4()}/done").status_code == 404
    assert api_client.post(f"/api/tasks/{uuid.uuid4()}/undo").status_code == 404


# ---------------------------------------------------------------------------
# What "done" means when the deadline outlives the task
# ---------------------------------------------------------------------------


def _parked_with_cleaning(session, car, *, days: list[int]) -> StreetSegmentSide:
    seg = StreetSegmentSide(
        street_name="Prospect Place",
        side=StreetSide.north,
        geom="SRID=4326;LINESTRING(-73.9715 40.679884, -73.9690 40.679884)",
    )
    session.add(seg)
    session.flush()
    session.add(
        AspRule(
            segment_side_id=seg.id,
            days_of_week=days,
            starts_at=time(8, 30),
            ends_at=time(10, 0),
            source=RuleSource.nyc_signs,
            confidence=1.0,
        )
    )
    parking = open_parking_session(session, vehicle=car, lat=LAT, lon=LON, at=NOW)
    confirm_side(session, parking_session=parking, segment_side=seg, confirmed_at=NOW)
    session.flush()
    return seg


def test_a_move_task_reopens_when_the_deadline_rolls_to_the_next_sweep(session, car):
    """The bug that would have made "done" dangerous.

    A move task is scoped to the parking session, but its ``due_by`` rolls
    forward to the next cleaning window once one passes — same row, new
    obligation. Ticked off for Monday and left done, this car would sit in one
    spot and never be warned again.
    """
    _parked_with_cleaning(session, car, days=[1, 4])  # ISO: Monday and Thursday
    first = refresh_move_task(session, vehicle=car, now=NOW)
    session.commit()
    monday = first.due_by
    assert monday is not None

    first.state = TaskState.done
    first.completed_at = NOW
    session.commit()

    # Past Monday's sweep: the same row now carries Thursday's deadline.
    after = refresh_move_task(session, vehicle=car, now=monday + timedelta(hours=2))
    session.commit()
    assert after is not None and after.id == first.id, "same row, by design"
    assert after.due_by is not None and after.due_by > monday, "a new sweep"
    assert after.state is TaskState.open, "a new deadline is a new obligation"
    assert after.completed_at is None


def test_a_done_move_task_stays_done_while_its_deadline_has_not_moved(session, car):
    """The other half. Re-opening on every poll would make "done" meaningless
    and put the task straight back on the sheet."""
    _parked_with_cleaning(session, car, days=[1, 4])
    task = refresh_move_task(session, vehicle=car, now=NOW)
    session.commit()
    task.state = TaskState.done
    task.completed_at = NOW
    session.commit()

    again = refresh_move_task(session, vehicle=car, now=NOW + timedelta(minutes=10))
    session.commit()
    assert again.state is TaskState.done
    assert again.completed_at is not None


def test_a_car_with_nothing_to_do_reports_an_empty_sheet(session, car, api_client):
    assert session.scalars(select(Task)).all() == []
    vehicle = api_client.get("/api/fleet").json()["vehicles"][0]
    assert vehicle["tasks"] == [] and vehicle["open_task_count"] == 0
