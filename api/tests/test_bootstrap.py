"""Boot-time fleet setup.

Two properties make it safe to leave switched on permanently, and both are
tested here: it is idempotent, and it cannot take the service down.
"""

from __future__ import annotations

import os

import httpx
import pytest
from sqlalchemy import func, select

from turonomics_api.bootstrap import apply_plates, parse_plate_map, run_bootstrap
from turonomics_api.db.models import Vehicle

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL") and not os.environ.get("DATABASE_URL"),
    reason="no database configured",
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Jimmy=LEH9892,Jolene=LWH4685", {"jimmy": "LEH9892", "jolene": "LWH4685"}),
        ("  Jimmy = leh-9892 ", {"jimmy": "LEH9892"}),  # normalised like any plate
        ("Jimmy=LEH9892,,garbage,=X,Y=", {"jimmy": "LEH9892"}),  # junk is dropped, not fatal
        ("", {}),
    ],
)
def test_plate_map_parsing(raw: str, expected: dict[str, str]) -> None:
    assert parse_plate_map(raw) == expected


def test_plates_are_applied_by_nickname(session) -> None:
    session.add(Vehicle(nickname="Jimmy", make="Toyota", model="4-Runner", year=2023))
    session.commit()
    changed = apply_plates(session, {"jimmy": "LEH9892"})
    session.commit()
    assert changed == ["Jimmy=LEH9892"]
    assert session.scalar(select(Vehicle)).plate == "LEH9892"


def test_a_plate_set_deliberately_is_not_overwritten_on_every_deploy(session) -> None:
    """The bootstrap runs on every boot. Someone correcting a plate in the app
    must not have it reverted by the next deploy."""
    session.add(
        Vehicle(nickname="Jimmy", make="Toyota", model="4-Runner", year=2023, plate="CORRECTED1")
    )
    session.commit()
    changed = apply_plates(session, {"jimmy": "LEH9892"})
    session.commit()
    assert changed == []
    assert session.scalar(select(Vehicle)).plate == "CORRECTED1"


def test_matching_also_works_on_the_bouncie_nickname(session) -> None:
    """Vehicles seeded from a device are named by Bouncie, and the operator may
    rename them locally afterwards."""
    session.add(
        Vehicle(
            nickname="Renamed", make="Toyota", model="Corolla", year=2025, bouncie_nickname="Jolene"
        )
    )
    session.commit()
    apply_plates(session, {"jolene": "LWH4685"})
    session.commit()
    assert session.scalar(select(Vehicle)).plate == "LWH4685"


def test_it_does_nothing_unless_switched_on(session, monkeypatch) -> None:
    monkeypatch.delenv("BOOTSTRAP_FLEET", raising=False)
    assert run_bootstrap() == 0


def test_a_bouncie_outage_does_not_stop_the_boot(session, monkeypatch) -> None:
    """The whole point of the non-fatal design: a dead provider costs a stale
    registry, not a service that will not start."""
    monkeypatch.setenv("BOOTSTRAP_FLEET", "true")
    monkeypatch.setenv("BOUNCIE_CLIENT_ID", "cid")
    monkeypatch.setenv("BOUNCIE_CLIENT_SECRET", "secret")
    monkeypatch.setenv("BOUNCIE_AUTH_CODE", "code")

    def dead(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "upstream is down"})

    monkeypatch.setattr(
        httpx, "Client", lambda *a, **k: httpx.Client(transport=httpx.MockTransport(dead))
    )
    # Must return rather than raise.
    assert run_bootstrap() == 0


def test_a_broken_database_url_does_not_stop_the_boot(session, monkeypatch) -> None:
    monkeypatch.setenv("BOOTSTRAP_FLEET", "1")
    monkeypatch.setattr(
        "turonomics_api.bootstrap.session_scope",
        lambda: (_ for _ in ()).throw(RuntimeError("no database")),
    )
    assert run_bootstrap() == 0


def test_running_it_twice_creates_nothing_the_second_time(session, monkeypatch) -> None:
    session.add(
        Vehicle(
            nickname="Jimmy",
            make="Toyota",
            model="4-Runner",
            year=2023,
            bouncie_imei="111111111111111",
        )
    )
    session.commit()
    before = session.scalar(select(func.count()).select_from(Vehicle))

    monkeypatch.setenv("BOOTSTRAP_FLEET", "true")
    monkeypatch.setenv("BOUNCIE_CLIENT_ID", "cid")
    monkeypatch.setenv("BOUNCIE_CLIENT_SECRET", "secret")
    monkeypatch.setenv("BOUNCIE_AUTH_CODE", "code")
    monkeypatch.setenv("BOOTSTRAP_PLATES", "Jimmy=LEH9892")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth/token":
            return httpx.Response(
                200, json={"access_token": "a", "refresh_token": "r", "expires_in": 3600}
            )
        return httpx.Response(
            200,
            json=[
                {
                    "nickName": "Jimmy",
                    "imei": "111111111111111",
                    "vin": "V1",
                    "model": {"make": "TOYOTA", "name": "4-Runner", "year": 2023},
                    "stats": {"lastUpdated": "2026-09-15T11:24:30.000Z"},
                }
            ],
        )

    monkeypatch.setattr(
        httpx, "Client", lambda *a, **k: httpx.Client(transport=httpx.MockTransport(handler))
    )
    run_bootstrap()
    run_bootstrap()
    assert session.scalar(select(func.count()).select_from(Vehicle)) == before
