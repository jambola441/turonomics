"""Where a rental went, and where on that route each toll was charged.

Two sources for the route, best first:

* **Bouncie's drives.** ``GET /v1/trips`` for the car's device over the
  rental, each drive with its start, end and GPS track. Bouncie refuses a
  range wider than a week, so a long rental is asked for in weekly slices.
* **The fleet's own telemetry**, the positions the poller has stored. Coarser
  — a fix every few minutes while driving — but on hand without a call out.

A crossing is placed on the route at the moment it was charged: the drive that
was under way then, at the fraction of that drive's duration that had passed.
That assumes an even speed within one drive, so it is an estimate along the
right road rather than a point on the gantry — said as such on the map.

Without a route, a crossing at a plaza this file knows is placed at the plaza.
Only the major crossings around the city are listed, at approximate positions,
and anything else is left off the map rather than guessed at.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from geoalchemy2.functions import ST_X, ST_Y
from geoalchemy2.types import Geometry
from sqlalchemy import cast, select
from sqlalchemy.orm import Session

from turonomics_api.db.models import TelemetryEvent

log = logging.getLogger("turonomics.ingest.trip_map")

# Bouncie's own limit on one request.
BOUNCIE_WINDOW = timedelta(days=7)
# A crossing a few minutes outside a drive — the statement's clock against the
# tracker's — is still that drive's.
ALIGN_WITHIN = timedelta(minutes=10)

# Approximate positions of the crossings this fleet uses, keyed by the plaza
# code on the E-ZPass statement. Only ones whose code is unambiguous; a code
# not here is not placed without a route.
PLAZAS: dict[str, tuple[float, float, str]] = {
    "VNB": (40.6066, -74.0447, "Verrazzano-Narrows Bridge"),
    "RKB": (40.7799, -73.9269, "RFK Bridge"),
    "BWB": (40.8013, -73.8290, "Bronx-Whitestone Bridge"),
    "TNB": (40.8003, -73.7932, "Throgs Neck Bridge"),
    "HHB": (40.8775, -73.9223, "Henry Hudson Bridge"),
    "BBT": (40.6960, -74.0135, "Hugh L. Carey Tunnel"),
    "QMT": (40.7440, -73.9643, "Queens-Midtown Tunnel"),
    "MPB": (40.5735, -73.8853, "Marine Parkway Bridge"),
    "CBB": (40.5960, -73.8217, "Cross Bay Bridge"),
    "GWB": (40.8517, -73.9527, "George Washington Bridge"),
    "HT": (40.7270, -74.0210, "Holland Tunnel"),
    "LT": (40.7623, -74.0110, "Lincoln Tunnel"),
    "CRZ": (40.7540, -73.9840, "Congestion relief zone (Manhattan below 60th St)"),
}


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


def bouncie_route(
    client: DrivesSource, imei: str, *, starts: datetime, ends: datetime
) -> Route:
    """The car's drives over a span, asked for a week at a time."""
    route = Route(source="bouncie")
    unreadable = 0
    cursor = starts
    while cursor < ends:
        upto = min(cursor + BOUNCIE_WINDOW - timedelta(seconds=1), ends)
        for raw in client.trips(
            imei,
            starts_after=cursor.strftime("%Y-%m-%dT%H:%M:%SZ"),
            ends_before=upto.strftime("%Y-%m-%dT%H:%M:%SZ"),
        ):
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


def along(points: Sequence[tuple[float, float]], fraction: float) -> tuple[float, float]:
    """The point a fraction of the way along a track, by distance."""
    if len(points) == 1:
        return points[0]
    fraction = min(1.0, max(0.0, fraction))
    legs = [_metres(points[i], points[i + 1]) for i in range(len(points) - 1)]
    total = sum(legs)
    if total == 0:
        return points[0]
    target = fraction * total
    for i, leg in enumerate(legs):
        if target <= leg or i == len(legs) - 1:
            t = 0.0 if leg == 0 else min(1.0, target / leg)
            a, b = points[i], points[i + 1]
            return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)
        target -= leg
    return points[-1]


@dataclass(frozen=True)
class Placed:
    lat: float
    lon: float
    # "route" — on the car's track at that moment; "plaza" — at the plaza's
    # approximate position.
    how: str


def place(occurred_at: datetime, plaza: str, route: Route) -> Placed | None:
    """Where on the map a crossing goes, or None if nothing honest can say."""
    for drive in route.drives:
        if drive.starts_at - ALIGN_WITHIN <= occurred_at <= drive.ends_at + ALIGN_WITHIN:
            span = (drive.ends_at - drive.starts_at).total_seconds()
            fraction = (
                0.5 if span <= 0 else (occurred_at - drive.starts_at).total_seconds() / span
            )
            lat, lon = along(drive.points, fraction)
            return Placed(lat=lat, lon=lon, how="route")
    known = PLAZAS.get(plaza.strip().upper())
    if known is not None:
        return Placed(lat=known[0], lon=known[1], how="plaza")
    return None
