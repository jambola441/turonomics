"""Where a rental went, and where on that route each toll was charged.

Two sources for the route, best first:

* **Bouncie's drives.** ``GET /v1/trips`` for the car's device over the
  rental, each drive with its start, end and GPS track. Bouncie refuses a
  range wider than a week, so a long rental is asked for in weekly slices.
* **The fleet's own telemetry**, the positions the poller has stored. Coarser
  — a fix every few minutes while driving — but on hand without a call out.

Crossings go at their plaza (``ingest/plazas.py``) and nowhere else. An
earlier version estimated where the car was at the moment of charge by
splitting a drive's duration evenly along its track; traffic and stops make
that miles wrong, and a guess drawn as a position is worse than no marker. A
plaza this app does not know is listed, not placed.

The route still checks the plazas, without timing: a plaza more than a few
kilometres from anywhere the car drove during the rental is the wrong plaza
for that crossing — a code meaning something else on another road — and is
flagged.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

import httpx
from geoalchemy2.functions import ST_X, ST_Y
from geoalchemy2.types import Geometry
from sqlalchemy import cast, select
from sqlalchemy.orm import Session

from turonomics_api.bouncie.client import BouncieClient, BouncieError
from turonomics_api.db.models import TelemetryEvent, Trip
from turonomics_api.ingest.plazas import locate
from turonomics_api.settings import toll_overrun_minutes

log = logging.getLogger("turonomics.ingest.trip_map")

# Bouncie's own limit on one request.
BOUNCIE_WINDOW = timedelta(days=7)

# How far a plaza may sit from the nearest point the car drove past before it is
# flagged. Tracks are sampled, not continuous, so a gantry between two fixes on
# a fast road can be a kilometre or two from both; beyond this, the car was not
# there.
DISAGREE_KM = 3.0


class DrivesSource(Protocol):
    def trips(
        self, imei: str, *, starts_after: str | None = None, ends_before: str | None = None
    ) -> list[dict[str, Any]]: ...


@dataclass(frozen=True)
class Drive:
    starts_at: datetime
    ends_at: datetime
    # [lat, lon] pairs, in order of travel.
    points: list[tuple[float, float]]
    # Bouncie's own figures for the drive, where it gave them.
    distance_mi: float | None = None
    start_odometer: float | None = None
    end_odometer: float | None = None


def _when(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def decode_polyline(text: str, precision: int = 5) -> list[tuple[float, float]]:
    """Google's encoded polyline, which Bouncie offers as its other GPS format."""
    points: list[tuple[float, float]] = []
    index = lat = lon = 0
    factor = 10**precision
    while index < len(text):
        for axis in (0, 1):
            shift = result = 0
            while True:
                if index >= len(text):
                    return points
                byte = ord(text[index]) - 63
                index += 1
                result |= (byte & 0x1F) << shift
                shift += 5
                if byte < 0x20:
                    break
            delta = ~(result >> 1) if result & 1 else result >> 1
            if axis == 0:
                lat += delta
            else:
                lon += delta
        points.append((lat / factor, lon / factor))
    return points


def _points(gps: Any) -> list[tuple[float, float]]:
    """A drive's track as [lat, lon] pairs, from whichever shape Bouncie sent."""
    if isinstance(gps, str):
        return decode_polyline(gps)
    if isinstance(gps, Mapping):
        coordinates = gps.get("coordinates")
        if isinstance(coordinates, list):
            # GeoJSON is [lon, lat].
            return [
                (float(c[1]), float(c[0]))
                for c in coordinates
                if isinstance(c, list | tuple)
                and len(c) >= 2
                and all(isinstance(v, int | float) for v in c[:2])
            ]
    if isinstance(gps, list):
        out: list[tuple[float, float]] = []
        for p in gps:
            if isinstance(p, Mapping):
                lat, lon = p.get("lat"), p.get("lon", p.get("lng"))
                if isinstance(lat, int | float) and isinstance(lon, int | float):
                    out.append((float(lat), float(lon)))
        return out
    return []


def parse_drive(raw: Mapping[str, Any]) -> Drive | None:
    starts, ends = _when(raw.get("startTime")), _when(raw.get("endTime"))
    if starts is None or ends is None or ends < starts:
        return None
    points = _points(raw.get("gps"))
    if not points:
        return None
    return Drive(
        starts_at=starts,
        ends_at=ends,
        points=points,
        distance_mi=_number(raw.get("distance")),
        start_odometer=_number(raw.get("startOdometer")),
        end_odometer=_number(raw.get("endOdometer")),
    )


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


# Bumped whenever `rental_miles` changes how it counts, so miles stored under
# an older rule are read again. 1 counted only drives starting inside the
# booking; 2 counted drives that overlap it, by the odometer span; 3 adds up
# the drives' own distances.
TRACKER_MILES_RULE = 3


def rental_miles(route: Route, *, starts: datetime, ends: datetime) -> float | None:
    """Miles the tracker recorded over a rental, or None if it cannot say.

    Drives that overlap the rental, not only those that start inside it. A
    guest who collects the car early starts the first drive before the booked
    time, and counting only drives that start inside the booking dropped it:
    Jason's 235-mile first drive, Ian Alexander's 205, Erica's 101, each read as
    a gap of that size against Turo. A drive of the host's that ends before the
    rental begins, or starts after it ends, is still left out.

    Each drive's own distance, added up: Bouncie's `distance`, or its end
    odometer less its start where that is missing. Not the odometer span from
    the first drive to the last, which was the rule before: on Danny's rental,
    Jolene's first weeks with the tracker, the odometer moved 178 miles between
    drives with no drive recorded, and the span read 675 against Turo's 493
    (the drives themselves added up to 497). A jump in the tracker's odometer
    is not miles the guest drove.
    """
    if route.source != "bouncie":
        return None
    drives = [d for d in route.drives if d.ends_at > starts and d.starts_at < ends]
    if not drives:
        return None
    total = 0.0
    for drive in drives:
        if drive.distance_mi is not None:
            total += drive.distance_mi
        elif (
            drive.start_odometer is not None
            and drive.end_odometer is not None
            and drive.end_odometer >= drive.start_odometer
        ):
            total += drive.end_odometer - drive.start_odometer
        else:
            # A drive with no distance at all: a figure without it would be
            # short by an unknown amount, which is worse than no figure.
            return None
    return round(total, 1)


@dataclass
class Route:
    drives: list[Drive] = field(default_factory=list)
    # "bouncie", "telemetry", or None.
    source: str | None = None
    note: str | None = None


def shape(value: Any, depth: int = 0) -> str:
    """A value's layout without its contents: keys, types and list lengths.

    For the log, so Bouncie's real response can be read without anyone handing
    over credentials, and without a guest's route ending up in it.
    """
    # Deep enough for a GeoJSON coordinate pair (drive → gps → coordinates →
    # point → number), which is the part this exists to see.
    if depth > 5:
        return "…"
    if isinstance(value, Mapping):
        inner = ", ".join(f"{k}: {shape(v, depth + 1)}" for k, v in list(value.items())[:40])
        return "{" + inner + "}"
    if isinstance(value, list):
        return f"[{len(value)} × {shape(value[0], depth + 1)}]" if value else "[]"
    if isinstance(value, str):
        return f"str({len(value)})"
    if value is None:
        return "null"
    return type(value).__name__


_SHAPE_LOGGED = False


def bouncie_route(
    client: DrivesSource, imei: str, *, starts: datetime, ends: datetime
) -> Route:
    """The car's drives over a span, asked for a week at a time."""
    global _SHAPE_LOGGED
    route = Route(source="bouncie")
    unreadable = 0
    cursor = starts
    while cursor < ends:
        upto = min(cursor + BOUNCIE_WINDOW - timedelta(seconds=1), ends)
        drives = client.trips(
            imei,
            starts_after=cursor.strftime("%Y-%m-%dT%H:%M:%SZ"),
            ends_before=upto.strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        if drives and not _SHAPE_LOGGED:
            # Once per process: the first real look at what Bouncie sends.
            log.info("bouncie trips: %d drive(s), first is %s", len(drives), shape(drives[0]))
            _SHAPE_LOGGED = True
        for raw in drives:
            drive = parse_drive(raw) if isinstance(raw, Mapping) else None
            if drive is None:
                unreadable += 1
            else:
                route.drives.append(drive)
        cursor = upto + timedelta(seconds=1)
    route.drives.sort(key=lambda d: d.starts_at)
    if unreadable:
        # Said, because the first real response is the first look at the
        # shape, and a map that is silently empty teaches nothing.
        route.note = f"{unreadable} drive(s) Bouncie returned could not be read"
    return route


def telemetry_route(
    session: Session, vehicle_id: Any, *, starts: datetime, ends: datetime
) -> Route:
    """The positions the poller stored, as one coarse drive."""
    geometry = cast(TelemetryEvent.location, Geometry)
    rows = session.execute(
        select(TelemetryEvent.occurred_at, ST_Y(geometry), ST_X(geometry))
        .where(
            TelemetryEvent.vehicle_id == vehicle_id,
            TelemetryEvent.location.is_not(None),
            TelemetryEvent.occurred_at >= starts,
            TelemetryEvent.occurred_at <= ends,
        )
        .order_by(TelemetryEvent.occurred_at)
    ).all()
    if len(rows) < 2:
        return Route(source=None)
    return Route(
        source="telemetry",
        drives=[
            Drive(
                starts_at=rows[0][0],
                ends_at=rows[-1][0],
                points=[(float(r[1]), float(r[2])) for r in rows],
            )
        ],
        note="from the tracker's stored positions — a fix every few minutes, not a full track",
    )


def _metres(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat = math.radians((a[0] + b[0]) / 2)
    dy = (b[0] - a[0]) * 111_320
    dx = (b[1] - a[1]) * 111_320 * math.cos(lat)
    return math.hypot(dx, dy)


def _segment_metres(
    p: tuple[float, float], a: tuple[float, float], b: tuple[float, float]
) -> float:
    """Distance from a point to a straight leg of a track, in metres.

    On a local flat projection, which is accurate to well under a percent at
    the few-kilometre scale this is used at.
    """
    lat = math.radians(p[0])
    kx, ky = 111_320 * math.cos(lat), 111_320

    def xy(q: tuple[float, float]) -> tuple[float, float]:
        return ((q[1] - p[1]) * kx, (q[0] - p[0]) * ky)

    ax, ay = xy(a)
    bx, by = xy(b)
    dx, dy = bx - ax, by - ay
    length = dx * dx + dy * dy
    t = 0.0 if length == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / length))
    return math.hypot(ax + t * dx, ay + t * dy)


def nearest_km(route: Route, point: tuple[float, float]) -> float | None:
    """How close the car came to a point at any time on the route, or None
    when there is no route to say."""
    best: float | None = None
    for drive in route.drives:
        pts = drive.points
        legs = zip(pts, pts[1:], strict=False) if len(pts) > 1 else [(pts[0], pts[0])]
        for a, b in legs:
            d = _segment_metres(point, a, b)
            best = d if best is None or d < best else best
    return None if best is None else best / 1000


@dataclass(frozen=True)
class Placed:
    lat: float
    lon: float
    # "plaza" — at the researched plaza; "zone" — the middle of a charging
    # zone, which is not a gantry.
    how: str
    name: str | None = None
    source: str | None = None
    # Set when the car's route never came within DISAGREE_KM of the plaza:
    # how close it got, in kilometres. The plaza is worth a second look.
    off_route_km: float | None = None
    # The research's confidence in the point: high, medium or low. Carried so
    # that something shown to a guest can refuse a plaza that was a guess.
    confidence: str | None = None


def place(plaza: str, route: Route, agency: str | None = None) -> Placed | None:
    """Where on the map a crossing goes: its plaza, or nowhere.

    The route checks a plaza but never moves the marker. A zone charge is
    not checked: its point is only the zone's middle, which a car can drive
    all over without passing.
    """
    found = locate(plaza, agency)
    if found is None:
        return None
    off: float | None = None
    if not found.zone:
        gap = nearest_km(route, (found.lat, found.lon))
        if gap is not None and gap > DISAGREE_KM:
            off = round(gap, 1)
    return Placed(
        lat=found.lat,
        lon=found.lon,
        how="zone" if found.zone else "plaza",
        name=found.name,
        source=found.source,
        off_route_km=off,
        confidence=found.confidence,
    )


# The route is read from a little before the booking to the end of the grace
# the toll matcher allows, so a crossing on the way back is on the map too.
MAP_BEFORE = timedelta(minutes=30)


def trip_route(session: Session, trip: Trip) -> Route:
    """A rental's route: Bouncie's drives, or the stored positions failing that.

    Never raises for want of a tracker or a Bouncie that did not answer; the
    reason goes in ``note``. A map is not worth an error page, and neither is
    the evidence image for an invoice.
    """
    starts = trip.starts_at - MAP_BEFORE
    ends = trip.ends_at + timedelta(minutes=toll_overrun_minutes())

    route = Route()
    imei = trip.vehicle.bouncie_imei if trip.vehicle else None
    if imei:
        try:
            route = bouncie_route(BouncieClient(session), imei, starts=starts, ends=ends)
        except (BouncieError, httpx.HTTPError, RuntimeError) as error:
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
    return route
