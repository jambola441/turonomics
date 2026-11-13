"""New York City parking and camera tickets on the fleet's plates.

From the city's "Open Parking and Camera Violations" data (NYC Open Data,
``nc67-uf89``), read by plate: parking tickets, and the school-zone speed,
red-light and bus-lane cameras. A police stop is issued to the driver rather
than the plate and is never here, and nor is anything outside the city.

Each read updates the tickets already kept, by the city's summons number,
because a ticket changes after it is written: penalties and interest are
added, it is paid or reduced. A new one is billed the way a toll is, to the
rental that had the car at that minute or was returned late past it (the toll
code's own rule, so the two can never disagree about who had the car). One
that no rental claims is tried again on every read, so a rental typed in or
pulled from Turo later still picks up its tickets.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from turonomics_api.db.models import Ticket, Trip, TripState, Vehicle
from turonomics_api.ingest.tolls import _claim
from turonomics_api.plates import normalize_plate
from turonomics_api.settings import fleet_timezone

log = logging.getLogger("turonomics.ingest.tickets")

DATASET = "https://data.cityofnewyork.us/resource/nc67-uf89.json"

# The city's data is days behind the street and changes slowly once there.
SYNC_EVERY = timedelta(hours=6)


def kind_of(violation: str) -> str:
    """What sort of ticket, from the city's wording of the violation."""
    text = violation.upper()
    if "SPEED" in text:
        return "speed_camera"
    if "RED LIGHT" in text:
        return "red_light_camera"
    if "BUS LANE" in text or "MTA CAMERA" in text:
        return "bus_lane_camera"
    return "parking"


def _cents(value: Any) -> int:
    try:
        return round(float(value) * 100)
    except (TypeError, ValueError):
        return 0


_TIME = re.compile(r"^(\d{1,2}):?(\d{2})\s*([AP])", re.IGNORECASE)


def issued_at(issue_date: str, violation_time: str | None) -> tuple[datetime, bool]:
    """When it was written, absolute, and whether the time of day is known.

    "09/08/2026" and "06:01P" are New York time. "12:30A" is half past
    midnight. With no usable time it is the start of that day, not known.
    """
    day = datetime.strptime(issue_date[:10], "%m/%d/%Y").date()
    match = _TIME.match((violation_time or "").strip())
    if match is None:
        return datetime.combine(day, time(0, 0), tzinfo=fleet_timezone()), False
    hour, minute, half = int(match.group(1)), int(match.group(2)), match.group(3).upper()
    if not (1 <= hour <= 12 and minute < 60):
        return datetime.combine(day, time(0, 0), tzinfo=fleet_timezone()), False
    hour = hour % 12 + (12 if half == "P" else 0)
    return datetime.combine(day, time(hour, minute), tzinfo=fleet_timezone()), True


@dataclass
class TicketRow:
    summons_number: str
    plate: str
    plate_state: str | None
    violation: str
    issued_at: datetime
    time_known: bool
    county: str | None
    precinct: str | None
    issuing_agency: str | None
    fine_cents: int
    penalty_cents: int
    interest_cents: int
    reduction_cents: int
    paid_cents: int
    due_cents: int
    status: str | None
    image_url: str | None


def parse(row: dict[str, Any]) -> TicketRow | None:
    """One row of the city's data, or None for one without what it needs."""
    summons = str(row.get("summons_number") or "").strip()
    plate = normalize_plate(str(row.get("plate") or ""))
    if not summons or not plate or not row.get("issue_date"):
        return None
    try:
        when, known = issued_at(str(row["issue_date"]), row.get("violation_time"))
    except ValueError:
        return None
    image = row.get("summons_image")
    url = image.get("url") if isinstance(image, dict) else None

    def text(key: str, most: int) -> str | None:
        value = str(row.get(key) or "").strip()
        return value[:most] or None

    return TicketRow(
        summons_number=summons[:20],
        plate=plate,
        plate_state=text("state", 4),
        violation=text("violation", 80) or "UNKNOWN",
        issued_at=when,
        time_known=known,
        county=text("county", 20),
        precinct=text("precinct", 10),
        issuing_agency=text("issuing_agency", 60),
        fine_cents=_cents(row.get("fine_amount")),
        penalty_cents=_cents(row.get("penalty_amount")),
        interest_cents=_cents(row.get("interest_amount")),
        reduction_cents=_cents(row.get("reduction_amount")),
        paid_cents=_cents(row.get("payment_amount")),
        due_cents=_cents(row.get("amount_due")),
        status=text("violation_status", 60),
        image_url=url if isinstance(url, str) else None,
    )


def fetch_tickets(
    plates: list[str], *, http: httpx.Client | None = None, app_token: str | None = None
) -> list[dict[str, Any]]:
    """Every ticket the city has on these plates."""
    if not plates:
        return []
    quoted = ",".join("'" + p.replace("'", "") + "'" for p in plates)
    headers = {"X-App-Token": app_token} if app_token else {}
    client = http or httpx.Client(timeout=60.0)
    rows: list[dict[str, Any]] = []
    offset = 0
    while True:
        response = client.get(
            DATASET,
            params={"$where": f"plate in ({quoted})", "$order": "summons_number",
                    "$limit": 5000, "$offset": offset},
            headers=headers,
        )
        response.raise_for_status()
        page = response.json()
        if not isinstance(page, list):
            break
        rows.extend(page)
        if len(page) < 5000:
            break
        offset += 5000
    return rows


def _rental_for(session: Session, ticket: Ticket) -> Trip | None:
    """The rental a ticket is billed to, by the toll rule.

    A ticket with no time of day goes to a rental only if it had the car the
    whole of that day: anything less and the ticket may have been the host's.
    """
    if ticket.vehicle_id is None:
        return None
    if ticket.time_known:
        # The toll rule leaves cancelled rentals out already.
        return _claim(session, ticket.vehicle_id, ticket.issued_at)
    day_start = ticket.issued_at
    day_end = day_start + timedelta(days=1)
    return session.scalar(
        select(Trip).where(
            Trip.vehicle_id == ticket.vehicle_id,
            Trip.state != TripState.cancelled,
            Trip.starts_at <= day_start,
            Trip.ends_at >= day_end,
        )
    )


@dataclass
class SyncReport:
    rows: int = 0
    new: int = 0
    updated: int = 0
    billed: int = 0
    new_ids: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (f"{self.rows} tickets read: {self.new} new, {self.updated} changed, "
                f"{self.billed} billed to a rental")


def store(session: Session, rows: list[dict[str, Any]], *, now: datetime | None = None) -> SyncReport:
    """Keep what the city says, and bill what a rental claims."""
    now = now or datetime.now(UTC)
    report = SyncReport()
    by_plate = {
        normalize_plate(v.plate): v for v in session.scalars(select(Vehicle)) if v.plate
    }
    for raw in rows:
        parsed = parse(raw)
        if parsed is None:
            continue
        report.rows += 1
        ticket = session.scalar(
            select(Ticket).where(Ticket.summons_number == parsed.summons_number)
        )
        values = {k: v for k, v in parsed.__dict__.items() if k != "summons_number"}
        if ticket is None:
            ticket = Ticket(summons_number=parsed.summons_number, kind=kind_of(parsed.violation),
                            first_seen_at=now, last_seen_at=now, **values)
            vehicle = by_plate.get(parsed.plate)
            ticket.vehicle_id = vehicle.id if vehicle else None
            session.add(ticket)
            session.flush()
            report.new += 1
            report.new_ids.append(str(ticket.id))
        else:
            changed = any(getattr(ticket, k) != v for k, v in values.items())
            for k, v in values.items():
                setattr(ticket, k, v)
            ticket.last_seen_at = now
            if changed:
                report.updated += 1
    session.flush()
    report.billed = rematch_tickets(session)
    session.commit()
    log.info("%s", report.summary())
    return report


def rematch_tickets(session: Session) -> int:
    """Bill tickets no rental had claimed to the rental that now does."""
    billed = 0
    for ticket in session.scalars(
        select(Ticket).where(Ticket.trip_id.is_(None), Ticket.vehicle_id.is_not(None))
    ):
        trip = _rental_for(session, ticket)
        if trip is not None:
            ticket.trip_id = trip.id
            billed += 1
    return billed


_last_sync: datetime | None = None


def sync_tickets(
    session: Session,
    *,
    now: datetime | None = None,
    force: bool = False,
    fetch: Any = None,
) -> SyncReport | None:
    """Read the city's tickets for the fleet's plates, at most every few hours.

    Never raises: a city outage costs a few hours' delay, not the poll.
    """
    global _last_sync
    now = now or datetime.now(UTC)
    if not force and _last_sync is not None and now - _last_sync < SYNC_EVERY:
        return None
    plates = [normalize_plate(v.plate) for v in session.scalars(select(Vehicle)) if v.plate]
    try:
        rows = (fetch or fetch_tickets)(
            plates, app_token=os.environ.get("NYC_OPEN_DATA_APP_TOKEN") or None
        )
        report = store(session, rows, now=now)
    except Exception:  # noqa: BLE001 - the city's data is not worth a failed poll
        log.warning("ticket sync failed", exc_info=True)
        session.rollback()
        return None
    _last_sync = now
    return report


def billable_cents(ticket: Ticket) -> int:
    """What the guest is asked for: the fine as written, less any reduction.

    Penalties and interest are left out: they come from the host paying the
    city late, which is not the guest's doing.
    """
    return max(ticket.fine_cents - ticket.reduction_cents, 0)
