"""Boot-time setup: the vehicle registry and the street-cleaning rules.

Two properties make both safe to leave switched on permanently, and both are
tested here: they are idempotent, and they cannot take the service down.
"""

from __future__ import annotations

import os
from contextlib import contextmanager

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from turonomics_api.bootstrap import (
    apply_plates,
    parse_plate_map,
    run_bootstrap,
    run_sign_bootstrap,
)
from turonomics_api.db.models import StreetSegmentSide, Vehicle

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


# ---------------------------------------------------------------------------
# Sign bootstrap
# ---------------------------------------------------------------------------

CLEAN_SIGN = "NO PARKING (SANITATION BROOM SYMBOL) MONDAY THURSDAY 11:30AM-1PM <->"


def _sign(x: int, y: int, dist: int, side: str = "N") -> dict:
    return {
        "on_street": "BERGEN STREET",
        "from_street": "VANDERBILT AVENUE",
        "to_street": "CARLTON AVENUE",
        "side_of_street": side,
        "sign_description": CLEAN_SIGN,
        "distance_from_intersection": dist,
        "sign_x_coord": x,
        "sign_y_coord": y,
        "record_type": "Current",
    }


@pytest.fixture()
def boots_against_test_db(engine, monkeypatch):
    """``session_scope`` binds its engine at import time from ``DATABASE_URL``,
    which in a test run points at nothing. Point the bootstrap's own sessions
    at the test database so these exercise the real write path rather than the
    error handler.
    """
    maker = sessionmaker(bind=engine, expire_on_commit=False, future=True)

    @contextmanager
    def scope():
        s = maker()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    monkeypatch.setattr("turonomics_api.bootstrap.session_scope", scope)


@pytest.fixture()
def counted_fetch(monkeypatch):
    """Stands in for the Open Data call and counts how often it was made."""
    calls: list[tuple] = []

    def fake(bbox, **kw):
        calls.append(bbox)
        return [_sign(993000, 187000, 50), _sign(993200, 187010, 200)]

    monkeypatch.setattr("turonomics_api.bootstrap.fetch_signs", fake)
    return calls


def test_signs_are_not_loaded_unless_switched_on(session, monkeypatch, counted_fetch) -> None:
    monkeypatch.delenv("BOOTSTRAP_SIGNS", raising=False)
    assert run_sign_bootstrap() == 0
    assert counted_fetch == []


def test_signs_load_into_an_empty_database(session, monkeypatch, counted_fetch, boots_against_test_db) -> None:
    monkeypatch.setenv("BOOTSTRAP_SIGNS", "true")
    assert run_sign_bootstrap() == 1
    assert len(counted_fetch) == 1
    assert session.scalar(select(func.count()).select_from(StreetSegmentSide)) == 1


def test_a_restart_does_not_refetch_the_dataset(session, monkeypatch, counted_fetch, boots_against_test_db) -> None:
    """The service restarts on every deploy and the fetch is several thousand
    rows. Paying for it once is the difference between a 4s boot and a 10s one.
    """
    monkeypatch.setenv("BOOTSTRAP_SIGNS", "true")
    run_sign_bootstrap()
    assert run_sign_bootstrap() == 0
    assert len(counted_fetch) == 1


def test_force_reloads_over_existing_rules(session, monkeypatch, counted_fetch, boots_against_test_db) -> None:
    """How to pick up the dataset's own refresh without dropping the table."""
    monkeypatch.setenv("BOOTSTRAP_SIGNS", "true")
    run_sign_bootstrap()
    monkeypatch.setenv("BOOTSTRAP_SIGNS", "force")
    assert run_sign_bootstrap() == 1
    assert len(counted_fetch) == 2
    # Reloading replaces rather than duplicates.
    assert session.scalar(select(func.count()).select_from(StreetSegmentSide)) == 1


def test_an_open_data_outage_does_not_stop_the_boot(session, monkeypatch, boots_against_test_db) -> None:
    monkeypatch.setenv("BOOTSTRAP_SIGNS", "1")

    def dead(bbox, **kw):
        raise httpx.HTTPError("nyc open data is down")

    monkeypatch.setattr("turonomics_api.bootstrap.fetch_signs", dead)
    assert run_sign_bootstrap() == 0


def test_a_malformed_bbox_does_not_stop_the_boot(
    session, monkeypatch, counted_fetch, boots_against_test_db
) -> None:
    monkeypatch.setenv("BOOTSTRAP_SIGNS", "true")
    monkeypatch.setenv("BOOTSTRAP_SIGNS_BBOX", "991558,180860")
    assert run_sign_bootstrap() == 0
    assert counted_fetch == []


def test_the_bbox_can_be_overridden(session, monkeypatch, counted_fetch, boots_against_test_db) -> None:
    """11238 is the default because it is the fleet's zip, not because the
    loader is specific to it."""
    monkeypatch.setenv("BOOTSTRAP_SIGNS", "true")
    monkeypatch.setenv("BOOTSTRAP_SIGNS_BBOX", "1, 2,3 ,4")
    run_sign_bootstrap()
    assert counted_fetch == [(1, 2, 3, 4)]
