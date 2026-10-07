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

from geoalchemy2.functions import ST_X, ST_Y
from geoalchemy2.types import Geometry
from sqlalchemy import cast, select
from sqlalchemy.orm import Session

from turonomics_api.db.models import TelemetryEvent
from turonomics_api.ingest.plazas import locate

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
    return Drive(starts_at=starts, ends_at=ends, points=points) if points else None


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
    )
