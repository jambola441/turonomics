"""The fleet endpoints are behind TOLLS_TOKEN.

They say where every car is and which guest has it, and they let a caller
confirm a parking side and tick off work. They were open to anyone who could
reach the API.
"""

from __future__ import annotations

import uuid

import pytest

from turonomics_api.db.models import Task, TaskKind, TaskState, Vehicle

from .conftest import requires_db

pytestmark = requires_db

AUTH = {"Authorization": "Bearer s3cret"}


@pytest.fixture()
def task(session, monkeypatch) -> Task:
    monkeypatch.setenv("TOLLS_TOKEN", "s3cret")
    car = Vehicle(nickname="Jimmy", make="Toyota", model="4-Runner", year=2023)
    session.add(car)
    session.flush()
    row = Task(vehicle_id=car.id, kind=TaskKind.turnaround, state=TaskState.open,
               title="Top up the tank")
    session.add(row)
    session.commit()
    return row


def test_every_fleet_endpoint_refuses_a_request_without_the_token(api_client, task) -> None:
    car = task.vehicle_id
    calls = [
        ("get", "/api/fleet", None),
        ("get", f"/api/vehicles/{car}/spots", None),
        ("post", f"/api/tasks/{task.id}/done", None),
        ("post", f"/api/tasks/{task.id}/undo", None),
        ("post", f"/api/parking/{uuid.uuid4()}/confirm", {"segment_side_id": str(uuid.uuid4())}),
    ]
    for method, path, body in calls:
        kwargs = {"json": body} if body is not None else {}
        assert getattr(api_client, method)(path, **kwargs).status_code == 401, path
        wrong = getattr(api_client, method)(
            path, headers={"Authorization": "Bearer nope"}, **kwargs)
        assert wrong.status_code == 401, path


def test_with_the_token_they_work_as_before(api_client, session, task) -> None:
    fleet = api_client.get("/api/fleet", headers=AUTH)
    assert fleet.status_code == 200
    assert [v["nickname"] for v in fleet.json()["vehicles"]] == ["Jimmy"]
    done = api_client.post(f"/api/tasks/{task.id}/done", headers=AUTH)
    assert done.status_code == 200 and done.json()["tasks"] == []
    session.refresh(task)
    assert task.state is TaskState.done
