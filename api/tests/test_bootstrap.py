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

from turonomics_api.asp.ingest import (
    FORT_GREENE_PARK_BBOX,
    JIMMY_BBOX,
    SIGN_AREAS,
    ZIP_11238_BBOX,
)
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
        # A block inside whatever box was asked for, named after it, so each
        # area loads a block-side of its own.
        calls.append(bbox)
        x, y = (bbox[0] + bbox[2]) // 2, (bbox[1] + bbox[3]) // 2
        street = f"STREET {bbox[0]}"
        return [{**_sign(x, y, 50), "on_street": street},
                {**_sign(x + 200, y + 10, 200), "on_street": street}]

    monkeypatch.setattr("turonomics_api.bootstrap.fetch_signs", fake)
    return calls


def test_signs_are_not_loaded_unless_switched_on(session, monkeypatch, counted_fetch) -> None:
    monkeypatch.delenv("BOOTSTRAP_SIGNS", raising=False)
    assert run_sign_bootstrap() == 0
    assert counted_fetch == []


def test_signs_load_into_an_empty_database(session, monkeypatch, counted_fetch, boots_against_test_db) -> None:
    monkeypatch.setenv("BOOTSTRAP_SIGNS", "true")
    assert run_sign_bootstrap() == len(SIGN_AREAS)
    assert counted_fetch == [area.bbox for area in SIGN_AREAS]
    assert session.scalar(select(func.count()).select_from(StreetSegmentSide)) == len(SIGN_AREAS)


def test_a_restart_does_not_refetch_the_dataset(session, monkeypatch, counted_fetch, boots_against_test_db) -> None:
    """The service restarts on every deploy and the fetch is several thousand
    rows. Paying for it once is the difference between a 4s boot and a 10s one.
    """
    monkeypatch.setenv("BOOTSTRAP_SIGNS", "true")
    run_sign_bootstrap()
    assert run_sign_bootstrap() == 0
    assert len(counted_fetch) == len(SIGN_AREAS)


def test_a_new_area_is_fetched_on_its_own(session, monkeypatch, counted_fetch, boots_against_test_db) -> None:
    """11238 and Fort Greene were loaded long ago; adding the area around Jimmy
    must load just that on the next boot, fetching neither of the others again."""
    monkeypatch.setenv("BOOTSTRAP_SIGNS", "true")
    monkeypatch.setattr("turonomics_api.bootstrap.SIGN_AREAS", SIGN_AREAS[:1])
    run_sign_bootstrap()
    assert counted_fetch == [ZIP_11238_BBOX]
    monkeypatch.setattr("turonomics_api.bootstrap.SIGN_AREAS", SIGN_AREAS[:2])
    assert run_sign_bootstrap() == 1
    assert counted_fetch == [ZIP_11238_BBOX, FORT_GREENE_PARK_BBOX]
    monkeypatch.setattr("turonomics_api.bootstrap.SIGN_AREAS", SIGN_AREAS)
    assert run_sign_bootstrap() == 1
    assert counted_fetch == [ZIP_11238_BBOX, FORT_GREENE_PARK_BBOX, JIMMY_BBOX]


def test_the_areas_probes_do_not_overlap_each_others_boxes() -> None:
    """A probe another area's load can reach would mark this area loaded
    when it never was."""
    def inside(probe, box):
        return not (probe[2] < box[0] or probe[0] > box[2] or probe[3] < box[1] or probe[1] > box[3])
    for area in SIGN_AREAS:
        x0, y0, x1, y1 = area.probe
        assert area.bbox[0] <= x0 < x1 <= area.bbox[2] and area.bbox[1] <= y0 < y1 <= area.bbox[3]
        for other in SIGN_AREAS:
            if other is not area:
                assert not inside(area.probe, other.bbox), (area.name, other.name)


def test_jimmy_sits_ten_blocks_inside_its_box() -> None:
    """Where Jimmy parks, Flatbush Avenue at Bergen Street, with ten of the
    short blocks (about 260 feet each) on every side."""
    x0, y0, x1, y1 = JIMMY_BBOX
    jimmy = (990807, 187180)
    for gap in (jimmy[0] - x0, x1 - jimmy[0], jimmy[1] - y0, y1 - jimmy[1]):
        assert gap >= 10 * 260


def test_fort_greene_park_sits_four_blocks_inside_its_box() -> None:
    """The park's kerbs, from the signs on Myrtle, DeKalb, St Edwards and
    Washington Park, with room on every side."""
    x0, y0, x1, y1 = FORT_GREENE_PARK_BBOX
    park = (990250, 190540, 991600, 191920)
    assert x0 < park[0] - 1000 and x1 > park[2] + 1000
    assert y0 < park[1] - 1500 and y1 > park[3] + 1500


def test_force_reloads_over_existing_rules(session, monkeypatch, counted_fetch, boots_against_test_db) -> None:
    """How to pick up the dataset's own refresh without dropping the table."""
    monkeypatch.setenv("BOOTSTRAP_SIGNS", "true")
    run_sign_bootstrap()
    monkeypatch.setenv("BOOTSTRAP_SIGNS", "force")
    assert run_sign_bootstrap() == len(SIGN_AREAS)
    assert len(counted_fetch) == 2 * len(SIGN_AREAS)
    # Reloading replaces rather than duplicates.
    assert session.scalar(select(func.count()).select_from(StreetSegmentSide)) == len(SIGN_AREAS)


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


# ---------------------------------------------------------------------------
# Binding Turo listings from configuration
# ---------------------------------------------------------------------------


def test_a_listing_map_takes_ids_or_whole_urls():
    """You have the URL in your hand when you are looking at the listing."""
    from turonomics_api.bootstrap import parse_listing_map

    assert parse_listing_map("Jerry=3382060,Jolene=3218625") == {
        "jerry": "3382060",
        "jolene": "3218625",
    }
    assert parse_listing_map("Jerry=https://turo.com/your-car/3382060/") == {"jerry": "3382060"}
    assert parse_listing_map(
        "Jerry=https://turo.com/us/en/car-rental/united-states/brooklyn-ny/"
        "toyota/corolla/3382060?utm=x"
    ) == {"jerry": "3382060"}


def test_a_listing_map_ignores_anything_that_is_not_an_id():
    """A typo in an environment variable must not become a binding."""
    from turonomics_api.bootstrap import parse_listing_map

    assert parse_listing_map("Jerry=notanumber") == {}
    assert parse_listing_map("Jerry") == {}
    assert parse_listing_map("") == {}


def test_binding_attaches_a_listing_to_the_named_car(session):
    from turonomics_api.bootstrap import apply_listings

    session.add_all(
        [
            Vehicle(nickname="Jerry", make="Toyota", model="Corolla", year=2025),
            Vehicle(nickname="Jolene", make="Toyota", model="Corolla", year=2025),
        ]
    )
    session.flush()
    changed = apply_listings(session, {"jerry": "3382060", "jolene": "3218625"})
    session.flush()
    assert sorted(changed) == ["Jerry=3382060", "Jolene=3218625"]
    rows = {v.nickname: v.turo_listing_id for v in session.scalars(select(Vehicle))}
    assert rows == {"Jerry": "3382060", "Jolene": "3218625"}


def test_binding_is_idempotent(session):
    """It runs on every deploy. Re-reporting a binding that was already there
    would make the log say something changed when nothing did."""
    from turonomics_api.bootstrap import apply_listings

    car = Vehicle(nickname="Jerry", make="Toyota", model="Corolla", year=2025)
    session.add(car)
    session.flush()
    assert apply_listings(session, {"jerry": "3382060"}) == ["Jerry=3382060"]
    session.flush()
    assert apply_listings(session, {"jerry": "3382060"}) == []


def test_two_cars_cannot_claim_one_listing(session):
    """The unique index would reject it — with a stack trace at boot, which is
    a bad trade for a convenience. Refused and logged instead."""
    from turonomics_api.bootstrap import apply_listings

    session.add_all(
        [
            Vehicle(nickname="Jerry", make="Toyota", model="Corolla", year=2025),
            Vehicle(nickname="Jolene", make="Toyota", model="Corolla", year=2025),
        ]
    )
    session.flush()
    changed = apply_listings(session, {"jerry": "3382060", "jolene": "3382060"})
    session.flush()
    assert changed == ["Jerry=3382060"]
    rows = {v.nickname: v.turo_listing_id for v in session.scalars(select(Vehicle))}
    assert rows == {"Jerry": "3382060", "Jolene": None}


def test_an_existing_binding_is_not_overwritten_by_a_deploy(session):
    """Rebinding moves every future trip for that listing onto a different car.
    That is deliberate enough to want a human at a shell, not an env var."""
    from turonomics_api.bootstrap import apply_listings

    car = Vehicle(nickname="Jerry", make="Toyota", model="Corolla", year=2025)
    car.turo_listing_id = "1111111"
    session.add(car)
    session.flush()
    assert apply_listings(session, {"jerry": "3382060"}) == []
    session.flush()
    assert session.scalars(select(Vehicle)).one().turo_listing_id == "1111111"


def test_run_bootstrap_applies_turo_listings(
    session, monkeypatch, boots_against_test_db
) -> None:
    """The wiring, not just the function.

    ``apply_listings`` can be perfect and the binding still never happen, which
    is exactly the bug this project shipped one module earlier: the mail sync
    passed the markup to the parser and nothing checked that it did, so setting
    it to None left every test green and the feature dead.
    """
    session.add_all(
        [
            Vehicle(nickname="Jerry", make="Toyota", model="Corolla", year=2025),
            Vehicle(nickname="Jolene", make="Toyota", model="Corolla", year=2025),
        ]
    )
    session.commit()

    monkeypatch.setenv("BOOTSTRAP_FLEET", "true")
    monkeypatch.setenv("TURO_LISTINGS", "Jerry=3382060,Jolene=3218625")
    # No Bouncie credentials: the sync raises, is caught, and the rest of the
    # bootstrap still has to run. That is the behaviour on a real deploy when
    # the Bouncie token has lapsed, so it is the right shape for this test.
    for name in ("BOUNCIE_CLIENT_ID", "BOUNCIE_CLIENT_SECRET", "BOUNCIE_AUTH_CODE"):
        monkeypatch.delenv(name, raising=False)

    run_bootstrap()

    session.expire_all()
    bound = {v.nickname: v.turo_listing_id for v in session.scalars(select(Vehicle))}
    assert bound == {"Jerry": "3382060", "Jolene": "3218625"}


def test_a_correct_binding_is_silent_on_the_next_deploy(session, caplog) -> None:
    """Idempotent *and* quiet. Falling through to the "already has a listing"
    branch would log a refusal for every correctly-bound car on every boot,
    which reads like a problem and trains you to ignore the log.
    """
    from turonomics_api.bootstrap import apply_listings

    car = Vehicle(nickname="Jerry", make="Toyota", model="Corolla", year=2025)
    car.turo_listing_id = "3382060"
    session.add(car)
    session.flush()
    with caplog.at_level("INFO", logger="turonomics.bootstrap"):
        assert apply_listings(session, {"jerry": "3382060"}) == []
    assert "already has listing" not in caplog.text
