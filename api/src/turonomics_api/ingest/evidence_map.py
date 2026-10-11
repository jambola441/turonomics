"""The car's tracker near each toll plaza, for the evidence sheet.

A toll invoice's evidence is a table of crossings. This adds, per plaza, a small
drawing of where the car's tracker was within a kilometre or so of it, so a
guest disputing a charge has something besides the app's word that the car went
that way.

What it will and will not claim, because the sheet goes to a guest and to Turo:

* **Only the track near the plaza is kept.** Each leg of the route is clipped to
  a circle around the plaza and everything outside the circle is dropped here,
  before the renderer sees it. A guest's whole route is theirs.
* **The plaza is where it is, not where the car was when charged.** Positions
  carry no timestamps per point (a drive has a start and an end), so nothing
  here says the car was at the plaza *at that minute*. The claim is "the tracker
  came within N metres of this plaza during the rental", and the sheet words it
  that way.
* **No guessed plazas.** Only plazas the research marks high or medium
  confidence, and not a zone's middle. A low-confidence row was a guess made
  good by the statement's agency, which is fine for a map the host reads and not
  for evidence a guest is asked to accept.
* **No track, no inset.** A plaza the tracker never came near — or any plaza at
  all when there is no route — leaves the sheet exactly as it was.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime

from turonomics_api.ingest.evidence import INSET_RADIUS_M, Inset
from turonomics_api.ingest.trip_map import Placed, Route, nearest_km, place

# The tracker must have come at least this close for the sheet to draw an inset.
# Tighter than the circle on purpose: the caption says the tracker "came within
# N m", and N should be a distance a guest would accept as having passed the
# plaza. A track between this and the circle's edge is in the data but not
# evidence of anything, so that plaza stays a plain row. Not zero, because a
# sampled track can miss a gantry by a few hundred metres on a fast road.
NEAR_M = 1000.0

# Three rows of three. More plazas than this is a long trip across a region, and
# the sheet says how many it left out.
MAX_INSETS = 9

_METRES_PER_DEGREE = 111_320.0

Point = tuple[float, float]


def _to_xy(point: Point, centre: Point) -> tuple[float, float]:
    east = (point[1] - centre[1]) * _METRES_PER_DEGREE * math.cos(math.radians(centre[0]))
    north = (point[0] - centre[0]) * _METRES_PER_DEGREE
    return east, north


def _from_xy(xy: tuple[float, float], centre: Point) -> Point:
    lat = centre[0] + xy[1] / _METRES_PER_DEGREE
    lon = centre[1] + xy[0] / (_METRES_PER_DEGREE * math.cos(math.radians(centre[0])))
    return lat, lon


def clip_segment(
    a: Point, b: Point, centre: Point, radius_m: float
) -> tuple[Point, Point] | None:
    """The part of a straight leg that lies inside a circle, or None.

    Done on a flat projection around the centre, which is accurate to well under
    a percent at this scale. A leg with neither end inside can still cross the
    circle — a sparse track jumps kilometres between fixes — and that crossing
    is kept, which an "endpoints inside" test would silently lose.
    """
    ax, ay = _to_xy(a, centre)
    bx, by = _to_xy(b, centre)
    dx, dy = bx - ax, by - ay
    quad = dx * dx + dy * dy
    if quad == 0:
        return (a, b) if ax * ax + ay * ay <= radius_m * radius_m else None
    # |a + t(b - a)|^2 = r^2
    half_b = ax * dx + ay * dy
    c = ax * ax + ay * ay - radius_m * radius_m
    disc = half_b * half_b - quad * c
    if disc < 0:
        return None
    root = math.sqrt(disc)
    t0 = (-half_b - root) / quad
    t1 = (-half_b + root) / quad
    lo, hi = max(0.0, t0), min(1.0, t1)
    if lo > hi:
        return None
    return (
        _from_xy((ax + lo * dx, ay + lo * dy), centre),
        _from_xy((ax + hi * dx, ay + hi * dy), centre),
    )


def local_segments(route: Route, centre: Point, radius_m: float) -> list[list[Point]]:
    """Polylines of the route's legs inside the circle; nothing outside it."""
    out: list[list[Point]] = []
    for drive in route.drives:
        current: list[Point] | None = None
        for a, b in zip(drive.points, drive.points[1:], strict=False):
            clipped = clip_segment(a, b, centre, radius_m)
            if clipped is None:
                current = None
                continue
            start, end = clipped
            if current is not None and _same(current[-1], start):
                current.append(end)
            else:
                current = [start, end]
                out.append(current)
    return out


def _same(a: Point, b: Point) -> bool:
    return abs(a[0] - b[0]) < 1e-9 and abs(a[1] - b[1]) < 1e-9


# A crossing as the sheet knows it: the plaza code, the statement's agency
# (which settles codes more than one agency uses) and when it was charged.
Crossing = tuple[str, str | None, datetime]


@dataclass
class _Group:
    """Crossings at one plaza, which share an inset."""

    plaza: str
    spot: Placed
    times: list[datetime] = field(default_factory=list)


def build_insets(route: Route, crossings: Sequence[Crossing]) -> tuple[list[Inset], int]:
    """The insets for a set of crossings, and how many plazas did not fit.

    Crossings at one plaza share an inset, listing each time.
    """
    if not route.drives:
        return [], 0
    groups: dict[tuple[float, float], _Group] = {}
    for plaza, agency, when in crossings:
        spot = place(plaza, route, agency)
        if spot is None or spot.how != "plaza" or spot.confidence not in ("high", "medium"):
            continue
        key = (round(spot.lat, 5), round(spot.lon, 5))
        groups.setdefault(key, _Group(plaza=plaza, spot=spot)).times.append(when)

    insets: list[Inset] = []
    for (lat, lon), group in groups.items():
        gap_km = nearest_km(route, (lat, lon))
        if gap_km is None or gap_km * 1000 > NEAR_M:
            continue
        segments = local_segments(route, (lat, lon), INSET_RADIUS_M)
        if not segments:
            continue
        insets.append(
            Inset(
                plaza=group.plaza,
                name=group.spot.name,
                lat=lat,
                lon=lon,
                segments=segments,
                closest_m=int(round(gap_km * 1000)),
                times=sorted(group.times),
                approximate=group.spot.confidence == "medium",
            )
        )
    insets.sort(key=lambda i: i.times[0])
    return insets[:MAX_INSETS], max(0, len(insets) - MAX_INSETS)
