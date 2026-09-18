"""The poll that keeps the run sheet current.

The case that matters most here is the one that reached production: a stored
snapshot row with ``is_running`` NULL whose provider timestamp has not changed,
so no new row is ever written to carry the field. Both cars sat parked with no
session and no deadline while the value they needed was in every poll's
response. Engine state is therefore applied from the live payload on every
poll, and the first test below is that bug.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, time, timedelta

import pytest
from sqlalchemy import func, select

from turonomics_api.bouncie.sync import sync_vehicles
from turonomics_api.db.models import (
    AspRule,
    ParkingSession,
    RuleSource,
    StreetSegmentSide,
    StreetSide,
    Task,
    TelemetryEvent,
    Vehicle,
)
from turonomics_api.ingest.poller import DEFAULT_INTERVAL_MINUTES, interval_minutes

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL") and not os.environ.get("DATABASE_URL"),
    reason="no database configured",
)

# Jimmy's real reported fix, on the north side of Bergen St.
LAT, LON = 40.679884, -73.970193
IMEI = "111111111111111"
LAST_UPDATED = "2026-09-17T16:34:13.000Z"
PARKED_AT = datetime(2026, 9, 17, 16, 34, 13, tzinfo=UTC)


class FakeBouncie:
    """Stands in for the provider. ``vehicles()`` is all ``sync_vehicles`` uses."""

    def __init__(self, *, is_running: bool = False, last_updated: str = LAST_UPDATED):
        self.is_running = is_running
        self.last_updated = last_updated
        self.calls = 0

    def vehicles(self) -> list[dict]:
        self.calls += 1
        return [
            {
                "nickName": "Jimmy",
                "imei": IMEI,
                "vin": "V1",
                "model": {"make": "TOYOTA", "name": "4-Runner", "year": 2023},
                "stats": {
                    "lastUpdated": self.last_updated,
                    "isRunning": self.is_running,
                    "location": {"lat": LAT, "lon": LON, "heading": 98.0},
                    "fuelLevel": 65.3,
                    "odometer": 45942.9,
                },
            }
        ]


def _bergen(session) -> StreetSegmentSide:
    seg = StreetSegmentSide(
        street_name="Bergen St",
        from_cross_street="Carlton Ave",
        to_cross_street="Vanderbilt Ave",
        side=StreetSide.north,
        geom="SRID=4326;LINESTRING(-73.9715 40.679900, -73.9690 40.679900)",
    )
    session.add(seg)
    session.flush()
    session.add(
        AspRule(
            segment_side_id=seg.id,
            days_of_week=[1, 4],
            starts_at=time(11, 30),
            ends_at=time(13, 0),
            source=RuleSource.nyc_signs,
            confidence=1.0,
        )
    )
    session.flush()
    return seg


def _jimmy(session) -> Vehicle:
    v = Vehicle(
        nickname="Jimmy",
        make="Toyota",
        model="4-Runner",
        year=2023,
        plate="LEH9892",
        bouncie_imei=IMEI,
    )
    session.add(v)
    session.flush()
    return v


def test_a_stored_row_with_no_engine_state_still_gets_a_parking_session(session):
    """The production bug, exactly.

    The snapshot row for this provider timestamp already exists and predates
    the ``is_running`` column, so it holds NULL. Because the timestamp has not
    changed, no new row will ever be written to carry the value. If parking
    were derived from stored rows, or only when the log grew, this car would
    never get a session no matter how long it polled.
    """
    _bergen(session)
    v = _jimmy(session)
    session.add(
        TelemetryEvent(
            vehicle_id=v.id,
            event_type="statsSnapshot",
            occurred_at=PARKED_AT,
            location=f"SRID=4326;POINT({LON} {LAT})",
            is_running=None,  # the column did not exist when this was written
            payload={},
            provider_event_id=f"stats:{PARKED_AT.isoformat()}",
        )
    )
    session.commit()

    result = sync_vehicles(session, FakeBouncie())

    assert result.events == 0, "the row already exists, so the log must not grow"
    assert result.parked == 1, "but parking must still be derived from the live payload"
    ps = session.scalar(select(ParkingSession).where(ParkingSession.vehicle_id == v.id))
    assert ps is not None
    assert ps.started_at == PARKED_AT, "the clock starts when it parked, not when we noticed"


def test_polling_repeatedly_does_not_pile_up_sessions_or_tasks(session):
    """It runs every ten minutes forever, so non-accumulation matters most."""
    _bergen(session)
    _jimmy(session)
    client = FakeBouncie()
    for _ in range(5):
        sync_vehicles(session, client)

    assert client.calls == 5
    assert session.scalar(select(func.count()).select_from(ParkingSession)) == 1
    assert session.scalar(select(func.count()).select_from(TelemetryEvent)) == 1
    assert session.scalar(select(func.count()).select_from(Task)) <= 1


def test_a_car_that_has_driven_off_has_its_session_closed(session):
    _bergen(session)
    v = _jimmy(session)
    sync_vehicles(session, FakeBouncie())
    assert session.scalar(select(ParkingSession)).ended_at is None

    later = (PARKED_AT + timedelta(hours=2)).isoformat().replace("+00:00", ".000Z")
    sync_vehicles(session, FakeBouncie(is_running=True, last_updated=later))

    ps = session.scalar(select(ParkingSession).where(ParkingSession.vehicle_id == v.id))
    assert ps.ended_at is not None


def test_silence_about_engine_state_opens_nothing(session):
    """``isRunning`` absent means the provider did not say. Treating that as
    parked would start a deadline the operator never earned."""
    _bergen(session)
    _jimmy(session)

    class Silent(FakeBouncie):
        def vehicles(self):
            rows = super().vehicles()
            del rows[0]["stats"]["isRunning"]
            return rows

    result = sync_vehicles(session, Silent())
    assert result.parked == 0
    assert session.scalar(select(func.count()).select_from(ParkingSession)) == 0


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, DEFAULT_INTERVAL_MINUTES),
        ("", DEFAULT_INTERVAL_MINUTES),
        ("5", 5),
        ("0", 0),  # the off switch
        ("-3", 0),
        ("banana", DEFAULT_INTERVAL_MINUTES),  # junk must not silently stop polling
    ],
)
def test_interval_parsing(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv("SYNC_INTERVAL_MINUTES", raising=False)
    else:
        monkeypatch.setenv("SYNC_INTERVAL_MINUTES", raw)
    assert interval_minutes() == expected


# ---------------------------------------------------------------------------
# The external trigger
# ---------------------------------------------------------------------------


def test_the_sync_endpoint_is_shut_when_no_token_is_configured(client, monkeypatch):
    """Forgetting to set a token must close the door, not leave it open."""
    monkeypatch.delenv("SYNC_TOKEN", raising=False)
    assert client.post("/api/sync").status_code == 503


def test_the_sync_endpoint_rejects_a_wrong_token(client, monkeypatch):
    monkeypatch.setenv("SYNC_TOKEN", "the-real-token")
    assert client.post("/api/sync").status_code == 401
    assert client.post("/api/sync", headers={"Authorization": "Bearer wrong"}).status_code == 401
    # An empty bearer is not a pass.
    assert client.post("/api/sync", headers={"Authorization": "Bearer "}).status_code == 401
