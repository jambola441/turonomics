"""Fleet state for the UI.

One endpoint that answers "where is everything and what does it need", because
that is the question the run sheet is built from. Assembling it server-side
keeps the client from having to know how parked position is derived.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from turonomics_api.db.base import get_session
from turonomics_api.db.models import (
    ParkingSession,
    StreetSegmentSide,
    Task,
    TaskState,
    TelemetryEvent,
    Vehicle,
)
from turonomics_api.ingest.parking import SEARCH_RADIUS_M, confirm_side, resolve_side
from turonomics_api.ingest.tasks import active_trip, refresh_move_task

router = APIRouter(prefix="/api", tags=["fleet"])

# Annotated rather than a Depends() default: same behaviour, and it does not
# read as a mutable default argument to a linter.
DbSession = Annotated[Session, Depends(get_session)]


class Position(BaseModel):
    lat: float
    lon: float
    heading: float | None = None
    reported_at: datetime


class SideOption(BaseModel):
    id: uuid.UUID
    street_name: str
    side: str
    distance_m: float | None = None
    is_guess: bool = False


class ParkingState(BaseModel):
    session_id: uuid.UUID
    since: datetime
    confirmed: bool
    confirmed_side: str | None = None
    street_name: str | None = None
    must_move_by: datetime | None = None
    guess_confidence: float | None = None
    guess_is_ambiguous: bool = False
    options: list[SideOption] = []


class VehicleState(BaseModel):
    id: uuid.UUID
    nickname: str
    make: str
    model: str
    year: int
    plate: str | None
    has_tracker: bool
    position: Position | None = None
    fuel_percent: float | None = None
    odometer_miles: float | None = None
    battery_status: str | None = None
    on_trip_with: str | None = None
    trip_ends_at: datetime | None = None
    parking: ParkingState | None = None
    open_task_count: int = 0


class FleetResponse(BaseModel):
    as_of: datetime
    vehicles: list[VehicleState]
    untracked_count: int


def _latest_located_event(session: Session, vehicle_id: uuid.UUID) -> TelemetryEvent | None:
    return session.scalar(
        select(TelemetryEvent)
        .where(TelemetryEvent.vehicle_id == vehicle_id, TelemetryEvent.location.is_not(None))
        .order_by(TelemetryEvent.occurred_at.desc())
        .limit(1)
    )


def _latest_event(session: Session, vehicle_id: uuid.UUID) -> TelemetryEvent | None:
    return session.scalar(
        select(TelemetryEvent)
        .where(TelemetryEvent.vehicle_id == vehicle_id)
        .order_by(TelemetryEvent.occurred_at.desc())
        .limit(1)
    )


def _lat_lon(session: Session, table: str, row_id: uuid.UUID) -> tuple[float, float] | None:
    """Pull lat/lon out of a geography column.

    Done in SQL rather than by parsing WKB in Python: PostGIS already knows how,
    and the cast to geometry is what exposes ST_X/ST_Y.
    """
    row = session.execute(
        text(
            f"SELECT ST_Y(location::geometry) AS lat, ST_X(location::geometry) AS lon "  # noqa: S608
            f"FROM {table} WHERE id = :id AND location IS NOT NULL"
        ),
        {"id": str(row_id)},
    ).first()
    return (float(row.lat), float(row.lon)) if row else None


def _parking_state(session: Session, vehicle: Vehicle) -> ParkingState | None:
    parking = session.scalar(
        select(ParkingSession)
        .where(ParkingSession.vehicle_id == vehicle.id, ParkingSession.ended_at.is_(None))
        .order_by(ParkingSession.started_at.desc())
        .limit(1)
    )
    if parking is None:
        return None

    pos = _lat_lon(session, "parking_session", parking.id)
    options: list[SideOption] = []
    if pos is not None:
        guess = resolve_side(session, lat=pos[0], lon=pos[1],
                             needs_large_spot=vehicle.needs_large_spot)
        distance = func.ST_Distance(
            StreetSegmentSide.geom,
            func.ST_GeogFromText(f"SRID=4326;POINT({pos[1]} {pos[0]})"),
        ).label("distance_m")
        # Bounded by the same radius the guess uses. Without it, a vehicle far
        # from any signed block is offered the nearest segments anywhere in the
        # dataset — a car upstate gets handed Brooklyn kerbs to confirm, and
        # confirming one would produce a deadline for a street it is nowhere
        # near.
        nearby = session.execute(
            select(StreetSegmentSide, distance)
            .where(StreetSegmentSide.geom.is_not(None))
            .where(
                func.ST_DWithin(
                    StreetSegmentSide.geom,
                    func.ST_GeogFromText(f"SRID=4326;POINT({pos[1]} {pos[0]})"),
                    SEARCH_RADIUS_M,
                )
            )
            .order_by(distance)
            .limit(4)
        ).all()
        for seg, dist in nearby:
            options.append(SideOption(
                id=seg.id, street_name=seg.street_name, side=seg.side.value,
                distance_m=round(float(dist), 1),
                is_guess=guess.segment_side is not None and seg.id == guess.segment_side.id,
            ))

    confirmed = parking.segment_side
    return ParkingState(
        session_id=parking.id,
        since=parking.started_at,
        confirmed=parking.segment_side_id is not None,
        confirmed_side=confirmed.side.value if confirmed else None,
        street_name=confirmed.street_name if confirmed else None,
        must_move_by=parking.must_move_by,
        guess_confidence=parking.guess_confidence,
        guess_is_ambiguous=(parking.guess_confidence or 0) < 0.5,
        options=options,
    )


@router.get("/fleet", response_model=FleetResponse)
def get_fleet(session: DbSession) -> FleetResponse:
    now = datetime.now(UTC)
    states: list[VehicleState] = []
    untracked = 0

    for vehicle in session.scalars(
        select(Vehicle).where(Vehicle.is_active.is_(True)).order_by(Vehicle.nickname)
    ):
        if not vehicle.bouncie_imei:
            untracked += 1

        located = _latest_located_event(session, vehicle.id)
        latest = _latest_event(session, vehicle.id)

        position = None
        if located is not None:
            coords = _lat_lon(session, "telemetry_event", located.id)
            if coords:
                position = Position(lat=coords[0], lon=coords[1],
                                    heading=located.heading_deg,
                                    reported_at=located.occurred_at)

        trip = active_trip(session, vehicle.id, now=now)
        open_tasks = session.scalar(
            select(func.count()).select_from(Task).where(
                Task.vehicle_id == vehicle.id, Task.state == TaskState.open
            )
        ) or 0

        states.append(VehicleState(
            id=vehicle.id, nickname=vehicle.nickname, make=vehicle.make,
            model=vehicle.model, year=vehicle.year, plate=vehicle.plate,
            has_tracker=bool(vehicle.bouncie_imei),
            position=position,
            fuel_percent=latest.fuel_percent if latest else None,
            odometer_miles=latest.odometer_miles if latest else None,
            battery_status=latest.battery_status if latest else None,
            on_trip_with=trip.guest_name if trip else None,
            trip_ends_at=trip.ends_at if trip else None,
            parking=_parking_state(session, vehicle),
            open_task_count=int(open_tasks),
        ))

    return FleetResponse(as_of=now, vehicles=states, untracked_count=untracked)


class ConfirmRequest(BaseModel):
    segment_side_id: uuid.UUID


@router.post("/parking/{session_id}/confirm", response_model=VehicleState)
def confirm_parking_side(
    session_id: uuid.UUID,
    body: ConfirmRequest,
    session: DbSession,
) -> VehicleState:
    """Apply the operator's answer to 'which side are you on?'.

    This is the step that turns a coin-flip guess into a deadline, so it is the
    only thing that can produce one.
    """
    parking = session.get(ParkingSession, session_id)
    if parking is None or parking.ended_at is not None:
        raise HTTPException(404, "no open parking session with that id")

    side = session.get(StreetSegmentSide, body.segment_side_id)
    if side is None:
        raise HTTPException(404, "unknown street segment side")

    confirm_side(session, parking_session=parking, segment_side=side,
                 confirmed_at=datetime.now(UTC))
    vehicle = session.get(Vehicle, parking.vehicle_id)
    if vehicle is None:  # pragma: no cover - the FK makes this unreachable
        raise HTTPException(500, "parking session has no vehicle")
    refresh_move_task(session, vehicle=vehicle, now=datetime.now(UTC))
    session.commit()

    fleet = get_fleet(session)
    for v in fleet.vehicles:
        if v.id == vehicle.id:
            return v
    raise HTTPException(500, "vehicle vanished mid-request")
