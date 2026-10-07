"""Rentals this fleet made outside Turo.

Turo's own rentals arrive by email and nothing here has to be typed. A rental
arranged directly leaves no trace at all, which until now meant its tolls could
not be attributed to anybody: the crossing was real, the guest was real, and
the ledger showed money nobody owed.

So this is a small amount of typing to let the rest of the machinery work. A
manual trip is an ordinary ``Trip`` with ``source=manual`` — the same table the
toll matcher, the run sheet and the turnaround logic already read — rather than
a parallel notion of a rental that each of them would have to learn about.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from turonomics_api.bouncie.client import BouncieClient, BouncieError
from turonomics_api.db.base import get_session
from turonomics_api.db.models import (
    ExtensionCommand,
    ReimbursementInvoice,
    Toll,
    Trip,
    TripSource,
    TripState,
    Vehicle,
)
from turonomics_api.ingest.tolls import rematch_unattributed
from turonomics_api.ingest.trip_map import Route, bouncie_route, place, telemetry_route
from turonomics_api.ingest.turo_extras import photo_groups, thread
from turonomics_api.routers.tolls import require_token
from turonomics_api.settings import fleet_timezone, toll_overrun_minutes

router = APIRouter(prefix="/api/trips", tags=["trips"])

DbSession = Annotated[Session, Depends(get_session)]


class TripIn(BaseModel):
    """A rental to record. Times without a zone are fleet-local."""

    vehicle: str = Field(min_length=1, description="Nickname or plate")
    guest_name: str | None = None
    starts_at: datetime
    ends_at: datetime
    earnings_cents: int | None = Field(default=None, ge=0)

    @field_validator("starts_at", "ends_at")
    @classmethod
    def _localise(cls, value: datetime) -> datetime:
        """A bare "2026-10-04T14:00" is 2pm where the cars are.

        An HTML datetime-local input sends exactly that, with no zone. Reading
        it as UTC is the mistake that put every scraped toll four hours early,
        and it would be the same mistake here — against a window that decides
        who gets billed.
        """
        if value.tzinfo is None:
            return value.replace(tzinfo=fleet_timezone())
        return value


class TripRow(BaseModel):
    id: uuid.UUID
    vehicle_nickname: str | None
    guest_name: str | None
    starts_at: datetime
    ends_at: datetime
    source: str
    turo_trip_id: str | None = None
    state: str | None = None
    earnings_cents: int | None = None
    # How many crossings this rental is currently carrying, so a window typed
    # slightly wrong is visible as a rental that caught nothing.
    toll_count: int = 0


class TripsResponse(BaseModel):
    trips: list[TripRow]


class CreateResponse(BaseModel):
    trip: TripRow
    # Crossings that became attributable the moment this rental existed. The
    # whole point of typing it in, so it is reported rather than left to be
    # noticed.
    tolls_matched: int


def _vehicle_for(session: Session, name: str) -> Vehicle:
    wanted = name.strip()
    found = session.scalar(
        select(Vehicle).where(
            func.lower(Vehicle.nickname) == wanted.lower()
        )
    )
    if found is None:
        found = session.scalar(
            select(Vehicle).where(
                func.replace(func.upper(Vehicle.plate), " ", "")
                == wanted.upper().replace(" ", "")
            )
        )
    if found is None:
        known = [
            v.nickname or v.plate or str(v.id) for v in session.scalars(select(Vehicle))
        ]
        raise HTTPException(
            422, f"no car called {name!r}. Known: {', '.join(sorted(filter(None, known)))}"
        )
    return found


def _row(session: Session, trip: Trip) -> TripRow:
    count = (
        session.scalar(
            select(func.count()).select_from(Toll).where(Toll.trip_id == trip.id)
        )
        or 0
    )
    return TripRow(
        id=trip.id,
        vehicle_nickname=trip.vehicle.nickname if trip.vehicle else None,
        guest_name=trip.guest_name,
        starts_at=trip.starts_at,
        ends_at=trip.ends_at,
        source=str(trip.source),
        turo_trip_id=trip.turo_trip_id,
        state=str(trip.state),
        earnings_cents=trip.earnings_cents,
        toll_count=int(count),
    )


@router.get("", response_model=TripsResponse)
def list_trips(session: DbSession, manual_only: bool = True) -> TripsResponse:
    """Rentals, newest first. Manual ones by default.

    The Turo ones are not editable here and there are hundreds of them; what
    this list is for is seeing and correcting what was typed in by hand.
    """
    query = select(Trip).order_by(Trip.starts_at.desc()).limit(200)
    if manual_only:
        query = query.where(Trip.source == TripSource.manual)
    return TripsResponse(trips=[_row(session, t) for t in session.scalars(query)])


@router.post("", response_model=CreateResponse)
def create_trip(
    body: TripIn,
    session: DbSession,
    authorization: Annotated[str | None, Header()] = None,
) -> CreateResponse:
    """Record an off-platform rental, then attribute whatever it now covers."""
    require_token(authorization)
    if body.ends_at <= body.starts_at:
        # Caught here as well as by the table's check constraint, because the
        # constraint's error is a stack trace and this one is a sentence.
        raise HTTPException(422, "the rental has to end after it starts")

    vehicle = _vehicle_for(session, body.vehicle)
    now = datetime.now(tz=fleet_timezone())
    trip = Trip(
        vehicle_id=vehicle.id,
        guest_name=(body.guest_name or "").strip() or None,
        starts_at=body.starts_at,
        ends_at=body.ends_at,
        state=(
            TripState.completed
            if body.ends_at < now
            else TripState.active
            if body.starts_at <= now
            else TripState.upcoming
        ),
        source=TripSource.manual,
        earnings_cents=body.earnings_cents,
        synced_at=now,
    )
    session.add(trip)
    session.flush()

    matched = rematch_unattributed(session)
    session.commit()
    return CreateResponse(trip=_row(session, trip), tolls_matched=matched)


@router.delete("/{trip_id}")
def delete_trip(
    trip_id: uuid.UUID,
    session: DbSession,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, int]:
    """Remove a manual rental typed in wrongly.

    Only a manual one. The Turo rentals are a record of what happened, rebuilt
    from mail, and deleting one here would be undone by the next sync anyway.

    Crossings it had claimed go back to being unattributed rather than being
    deleted with it — the toll still happened.
    """
    require_token(authorization)
    trip = session.get(Trip, trip_id)
    if trip is None:
        # Same reasoning as deleting a toll: a retry after a dropped
        # connection is likelier than a delete aimed at nothing.
        return {"deleted": 0, "tolls_released": 0}
    if trip.source is not TripSource.manual:
        raise HTTPException(
            422, "only a manually recorded rental can be deleted here"
        )
    # Counted before the delete, not cleared by hand: toll.trip_id is declared
    # ON DELETE SET NULL, so the crossings are released by the database. A loop
    # doing it here too looked load-bearing and was not — mutating it away
    # changed nothing. What keeps that honest is the test asserting the tolls
    # still exist afterwards, which is what would fail if the constraint ever
    # became CASCADE.
    released = (
        session.scalar(
            select(func.count()).select_from(Toll).where(Toll.trip_id == trip.id)
        )
        or 0
    )
    session.delete(trip)
    session.commit()
    return {"deleted": 1, "tolls_released": int(released)}


# ---------------------------------------------------------------------------
# One rental, with everything linked to it
# ---------------------------------------------------------------------------


class Fact(BaseModel):
    label: str
    value: str


class ViewToll(BaseModel):
    id: uuid.UUID
    occurred_at: datetime
    plaza: str
    amount_cents: int
    filed_at: datetime | None
    recovered_at: datetime | None


class ViewInvoice(BaseModel):
    turo_invoice_id: str | None
    # Ours: filed / unanswered / charged, from the mail and the extension.
    state: str
    # Turo's own, from its invoice page where it has been read.
    turo_status: str | None
    total_cents: int
    toll_cents: int | None
    lines: list[Fact]
    first_seen_at: datetime | None
    charged_at: datetime | None
    url: str | None


class ViewPhotos(BaseModel):
    step: str
    count: int
    by: str | None
    first: datetime | None
    last: datetime | None


class ViewMessage(BaseModel):
    role: str | None
    name: str | None
    sent_at: datetime | None
    text: str | None
    images: int


class ViewCommand(BaseModel):
    kind: str
    state: str
    requested_at: datetime
    result: str | None


class TripView(BaseModel):
    trip: TripRow
    turo_trip_id: str | None
    plate: str | None
    state: str
    reservation_url: str | None
    invoice_hub_url: str | None
    # The ledger's row for it: the same figures and word the invoices page uses.
    ledger: dict[str, object] | None
    # Whether the site may offer to file it, and why not if not.
    fileable: bool
    held_because: str | None
    # Turo's reservation detail, as a short list of what a person reads, and
    # whole — everything else it said, for the questions nobody has asked yet.
    turo_synced_at: datetime | None
    turo_facts: list[Fact]
    turo_detail: dict[str, object] | None
    tolls: list[ViewToll]
    invoices: list[ViewInvoice]
    commands: list[ViewCommand]
    # The trip's photos, counted by step, and its message thread — None until
    # a pull has read them, which is different from a trip that had none.
    photos: list[ViewPhotos] | None = None
    messages: list[ViewMessage] | None = None
    extras_synced_at: datetime | None = None


_TURO = "https://turo.com/us/en"


def _get(data: object, *path: str) -> object:
    for key in path:
        if not isinstance(data, dict):
            return None
        data = data.get(key)
    return data


def _say(value: object) -> str | None:
    """A Turo value as a person reads it, or None if there is nothing to say.

    Turo's shapes repeat: money is {amount, currencyCode}, a distance is
    {scalar, unit, unlimited}, a moment is {epochMillis, localDate, localTime}.
    """
    if value is None or value == "" or value == []:
        return None
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (int, float, str)):
        return str(value)
    if isinstance(value, list):
        said = [s for s in (_say(v) for v in value) if s]
        return ", ".join(said) or None
    if isinstance(value, dict):
        if value.get("unlimited") is True:
            return "unlimited"
        if "amount" in value and isinstance(value["amount"], (int, float)):
            return f"${value['amount']:,.2f}"
        if "scalar" in value and value.get("scalar") is not None:
            return f"{value['scalar']:,} {str(value.get('unit') or '').lower()}".strip()
        if "localDate" in value:
            return f"{value.get('localDate')} {value.get('localTime') or ''}".strip()
        if "money" in value:
            money = _say(value.get("money"))
            distance = _say(value.get("distance"))
            return f"{money} per {distance}" if money and distance else money
    return None


# What a person reads first, in this order. Paths into Turo's
# /api/reservation/detail as observed; anything missing is skipped.
_FACTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Status", ("statusCode",)),
    ("Guest", ("renter", "name")),
    ("Starts", ("booking", "start")),
    ("Ends", ("booking", "end")),
    ("Booked", ("created",)),
    ("Trip price", ("booking", "costWithCurrency")),
    ("Distance included", ("booking", "distanceLimit")),
    ("Odometer at check-in", ("odometerDetail", "checkInOdometerReading")),
    ("Odometer at check-out", ("odometerDetail", "checkOutOdometerReading")),
    ("Latest odometer", ("odometerDetail", "latestOdometerReading")),
    ("Distance driven", ("odometerDetail", "distanceDriven")),
    ("Over the limit", ("odometerDetail", "excessDistance")),
    ("Overage rate", ("distanceOverageFee",)),
    ("Protection", ("protectionLevel",)),
    ("Check-in", ("reservationCheckInStatus",)),
    ("Pickup", ("booking", "location", "address")),
    ("Plate on Turo", ("booking", "vehicleRegistration", "licensePlate")),
    ("Instant book", ("instantBookable",)),
    ("Receipt available", ("receiptAvailable",)),
    ("Turo offers", ("reservationActions",)),
)


def turo_facts(detail: dict[str, object] | None) -> list[Fact]:
    if not detail:
        return []
    facts: list[Fact] = []
    for label, path in _FACTS:
        said = _say(_get(detail, *path))
        if said:
            facts.append(Fact(label=label, value=said))
    return facts


@router.get("/{trip_id}/view", response_model=TripView)
def view_trip(
    trip_id: uuid.UUID,
    session: DbSession,
    authorization: Annotated[str | None, Header()] = None,
) -> TripView:
    """Everything about one rental, Turo's side and ours.

    Behind the token, unlike the lists: Turo's detail carries the guest's
    name, the pickup address and the people on the account, and the API has
    no login of its own.
    """
    # Imported here: the invoices router is the larger of the two, and this is
    # the one place the trips router needs it.
    from turonomics_api.routers.invoices import _by_kind, fileable_invoice, ledger

    require_token(authorization)
    trip = session.get(Trip, trip_id)
    if trip is None:
        raise HTTPException(status_code=404, detail="no such rental")

    row = next((r for r in ledger(session).rows if r.trip_id == trip.id), None)
    held: str | None
    if trip.source is TripSource.manual:
        held = "off-platform — invoice the guest directly"
    else:
        _, held = fileable_invoice(session, trip.id)

    invoices = session.scalars(
        select(ReimbursementInvoice)
        .where(ReimbursementInvoice.trip_id == trip.id)
        .order_by(ReimbursementInvoice.first_seen_at)
    ).all()
    tolls = session.scalars(
        select(Toll).where(Toll.trip_id == trip.id).order_by(Toll.occurred_at)
    ).all()
    commands = session.scalars(
        select(ExtensionCommand)
        .where(ExtensionCommand.trip_id == trip.id)
        .order_by(ExtensionCommand.requested_at.desc())
        .limit(10)
    ).all()
    res = trip.turo_trip_id

    def _lines(invoice: ReimbursementInvoice) -> list[Fact]:
        out: list[Fact] = []
        for entry in invoice.lines or []:
            if isinstance(entry, list) and len(entry) == 2 and isinstance(entry[1], int):
                out.append(Fact(label=str(entry[0]), value=f"${entry[1] / 100:,.2f}"))
        return out

    ledger_row = row.model_dump(mode="json") if row is not None else None
    if ledger_row is None and invoices:
        # A rental the ledger does not list (upcoming, say) still has Turo's
        # side worth adding up.
        ledger_row = dict(_by_kind(list(invoices)))

    return TripView(
        trip=_row(session, trip),
        turo_trip_id=res,
        plate=trip.vehicle.plate if trip.vehicle else None,
        state=str(trip.state),
        reservation_url=f"{_TURO}/reservation/{res}" if res else None,
        invoice_hub_url=f"{_TURO}/reservation/{res}/invoice-hub" if res else None,
        ledger=ledger_row,
        fileable=held is None,
        held_because=held,
        turo_synced_at=trip.detail_synced_at,
        turo_facts=turo_facts(trip.turo_detail),
        turo_detail=trip.turo_detail,
        tolls=[
            ViewToll(
                id=t.id,
                occurred_at=t.occurred_at,
                plaza=t.plaza,
                amount_cents=t.amount_cents,
                filed_at=t.filed_at,
                recovered_at=t.recovered_at,
            )
            for t in tolls
        ],
        invoices=[
            ViewInvoice(
                turo_invoice_id=i.turo_invoice_id,
                state=i.state,
                turo_status=(
                    str(i.turo_body.get("reimbursementStatus"))
                    if isinstance(i.turo_body, dict) and i.turo_body.get("reimbursementStatus")
                    else None
                ),
                total_cents=i.total_cents,
                toll_cents=i.toll_cents,
                lines=_lines(i),
                first_seen_at=i.first_seen_at,
                charged_at=i.charged_at,
                url=(
                    f"{_TURO}/reservation/{res}/reimbursement/invoice?invoiceId={i.turo_invoice_id}"
                    if res and i.turo_invoice_id and i.turo_body is not None
                    else None
                ),
            )
            for i in invoices
        ],
        commands=[
            ViewCommand(
                kind=c.kind, state=c.state, requested_at=c.requested_at, result=c.result
            )
            for c in commands
        ],
        photos=None if trip.turo_photos is None else [
            ViewPhotos(step=g.step, count=g.count, by=g.by, first=g.first, last=g.last)
            for g in photo_groups(trip.turo_photos)
        ],
        messages=None if trip.turo_messages is None else [
            ViewMessage(role=m.role, name=m.name, sent_at=m.sent_at, text=m.text, images=m.images)
            for m in thread(trip.turo_messages)
        ],
        extras_synced_at=trip.extras_synced_at,
    )


# ---------------------------------------------------------------------------
# Where it went
# ---------------------------------------------------------------------------


class MapToll(BaseModel):
    occurred_at: datetime
    plaza: str
    amount_cents: int
    lat: float | None
    lon: float | None
    # "route": on the car's track at that moment. "plaza": at the plaza's
    # approximate position. None: not placed — listed, not guessed.
    how: str | None


class MapDrive(BaseModel):
    starts_at: datetime
    ends_at: datetime
    points: list[list[float]]


class TripMap(BaseModel):
    starts_at: datetime
    ends_at: datetime
    drives: list[MapDrive]
    tolls: list[MapToll]
    # "bouncie", "telemetry", or None when there is no route at all.
    route_source: str | None
    note: str | None


# The route is read from a little before the booking to the end of the grace
# the toll matcher allows, so a crossing on the way back is on the map too.
_MAP_BEFORE = timedelta(minutes=30)


@router.get("/{trip_id}/map", response_model=TripMap)
def trip_map(
    trip_id: uuid.UUID,
    session: DbSession,
    authorization: Annotated[str | None, Header()] = None,
) -> TripMap:
    """The rental's route and its crossings on it, behind the token: a car's
    movements are as private as anything Turo says about the guest."""
    require_token(authorization)
    trip = session.get(Trip, trip_id)
    if trip is None:
        raise HTTPException(status_code=404, detail="no such rental")
    starts = trip.starts_at - _MAP_BEFORE
    ends = trip.ends_at + timedelta(minutes=toll_overrun_minutes())

    route = Route()
    imei = trip.vehicle.bouncie_imei if trip.vehicle else None
    if imei:
        try:
            route = bouncie_route(BouncieClient(session), imei, starts=starts, ends=ends)
        except (BouncieError, httpx.HTTPError, RuntimeError) as error:
            # A map is not worth an error page. Fall back, and say why.
            route = Route(note=f"Bouncie did not answer ({error})")
    if not route.drives:
        fallback = telemetry_route(session, trip.vehicle_id, starts=starts, ends=ends)
        if fallback.drives:
            fallback.note = "; ".join(n for n in (route.note, fallback.note) if n)
            route = fallback
        elif route.source is None and not route.note:
            route.note = (
                "no tracker on this car" if not imei else "the tracker recorded no driving"
            )

    tolls = session.scalars(
        select(Toll).where(Toll.trip_id == trip.id).order_by(Toll.occurred_at)
    ).all()
    placed_tolls = []
    for toll in tolls:
        spot = place(toll.occurred_at, toll.plaza, route)
        placed_tolls.append(
            MapToll(
                occurred_at=toll.occurred_at,
                plaza=toll.plaza,
                amount_cents=toll.amount_cents,
                lat=spot.lat if spot else None,
                lon=spot.lon if spot else None,
                how=spot.how if spot else None,
            )
        )
    return TripMap(
        starts_at=trip.starts_at,
        ends_at=trip.ends_at,
        drives=[
            MapDrive(
                starts_at=d.starts_at,
                ends_at=d.ends_at,
                points=[[round(lat, 6), round(lon, 6)] for lat, lon in d.points],
            )
            for d in route.drives
        ],
        tolls=placed_tolls,
        route_source=route.source if route.drives else None,
        note=route.note,
    )
