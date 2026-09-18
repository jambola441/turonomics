"""Pull vehicle state from Bouncie into the registry.

This is the *pull* half of the integration, used for the initial seed and as a
backstop when webhooks are missed. Webhooks are the primary path.

Deliberately conservative about overwriting: Bouncie's nickname and the
registry nickname are different things (the operator may rename a car in one
and not the other), so a device-sourced nickname only fills a blank.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from turonomics_api.bouncie.client import BouncieClient
from turonomics_api.db.models import TelemetryEvent, Vehicle
from turonomics_api.ingest.parking import apply_engine_state
from turonomics_api.ingest.tasks import refresh_move_task


@dataclass
class SyncResult:
    matched: int = 0
    created: int = 0
    events: int = 0
    parked: int = 0
    unmatched_imeis: list[str] | None = None

    def __post_init__(self) -> None:
        if self.unmatched_imeis is None:
            self.unmatched_imeis = []


def _point(lat: float, lon: float) -> str:
    return f"SRID=4326;POINT({lon} {lat})"


def _parse_ts(value: str | None) -> datetime:
    if not value:
        return datetime.now(UTC)
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(UTC)


def sync_vehicles(
    session: Session, client: BouncieClient, *, create_missing: bool = False
) -> SyncResult:
    """Reconcile the registry against the devices on the account.

    ``create_missing`` is off by default: a device appearing on the account is
    not by itself proof that the operator wants a new fleet vehicle row, and a
    vehicle with no device is a first-class case rather than an error.
    """
    result = SyncResult()

    for payload in client.vehicles():
        imei = payload.get("imei")
        vin = payload.get("vin")
        stats = payload.get("stats") or {}

        vehicle = None
        if imei:
            vehicle = session.scalar(select(Vehicle).where(Vehicle.bouncie_imei == imei))
        if vehicle is None and vin:
            vehicle = session.scalar(select(Vehicle).where(Vehicle.vin == vin))

        if vehicle is None:
            if not create_missing:
                if imei:
                    assert result.unmatched_imeis is not None
                    result.unmatched_imeis.append(imei)
                continue
            model = payload.get("model") or {}
            vehicle = Vehicle(
                nickname=payload.get("nickName") or f"Vehicle {imei[-4:] if imei else '?'}",
                make=model.get("make", "Unknown").title(),
                model=model.get("name", "Unknown"),
                year=int(model.get("year") or 0) or 1900,
                # Bouncie does not know plates. Left unset rather than faked:
                # a placeholder would be a plate-shaped value that matches no
                # toll, which looks like "no tolls" instead of missing data.
                plate=None,
                vin=vin,
                bouncie_imei=imei,
            )
            session.add(vehicle)
            session.flush()
            result.created += 1
        else:
            result.matched += 1

        if vehicle.bouncie_imei is None and imei:
            vehicle.bouncie_imei = imei
        if vehicle.bouncie_nickname is None:
            vehicle.bouncie_nickname = payload.get("nickName")

        # A car whose device reports these is a car whose check-out can
        # auto-fill instead of asking for typing.
        vehicle.reports_fuel_level = stats.get("fuelLevel") is not None
        vehicle.reports_obd_odometer = stats.get("odometer") is not None

        occurred = _parse_ts(stats.get("lastUpdated"))
        # One snapshot row per distinct provider timestamp, so repeated polls
        # of an unmoved car do not pile up duplicates.
        marker = f"stats:{occurred.isoformat()}"
        existing = session.scalar(
            select(TelemetryEvent).where(
                TelemetryEvent.vehicle_id == vehicle.id,
                TelemetryEvent.provider_event_id == marker,
            )
        )
        if existing is None:
            loc = stats.get("location") or {}
            mil = stats.get("mil") or {}
            battery = stats.get("battery") or {}
            session.add(
                TelemetryEvent(
                    vehicle_id=vehicle.id,
                    event_type="statsSnapshot",
                    occurred_at=occurred,
                    location=_point(loc["lat"], loc["lon"]) if loc.get("lat") is not None else None,
                    heading_deg=loc.get("heading"),
                    speed_mph=stats.get("speed"),
                is_running=stats.get("isRunning"),
                    fuel_percent=stats.get("fuelLevel"),
                    odometer_miles=stats.get("odometer"),
                    battery_status=battery.get("status"),
                    mil_on=mil.get("milOn"),
                    dtc_count=len(mil.get("qualifiedDtcList") or []),
                    payload=payload,
                    provider_event_id=marker,
                )
            )
            result.events += 1

        # Engine state is applied on every poll, deliberately outside the
        # de-duplication above.
        #
        # Recording telemetry and deriving parking are different jobs. The
        # event row is an append-only log, so one row per provider timestamp is
        # right. Parking is a projection of where the car is *now*, and a
        # projection has to be rebuilt from current state whether or not the
        # log grew.
        #
        # Tying the two together cost the fleet a day: `is_running` was added
        # after those snapshot rows were written, so every stored row had it
        # NULL, and because the provider timestamp had not changed no new row
        # was ever written to carry it. Both cars sat parked with no session
        # and no deadline while the field they needed was in every poll's
        # response. Applying it from the live payload cannot get stuck that way.
        loc = stats.get("location") or {}
        if apply_engine_state(
            session,
            vehicle=vehicle,
            is_running=stats.get("isRunning"),
            lat=loc.get("lat"),
            lon=loc.get("lon"),
            at=occurred,
        ):
            result.parked += 1
        refresh_move_task(session, vehicle=vehicle)

    session.commit()
    return result
