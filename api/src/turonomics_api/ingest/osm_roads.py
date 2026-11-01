"""Roads and water around a plaza, from OpenStreetMap, for the evidence maps.

A track on a blank square says very little: nothing shows that a plaza is on a
bridge approach, or that a line 600 m away is the highway alongside it. This
fetches the major roads and the water inside each inset's circle so the sheet
can draw them faintly beneath the track, the roads named. In New York the water
is half of what places a plaza: a bridge or a tunnel is a road meeting a river.

Water comes two ways. A lake or pond wholly inside the query box is a closed
outline and is drawn filled. A river, a bay or the sea is far bigger than the
box, arrives cut to it, and cannot be filled from the pieces, so its shores
are drawn as lines: two blue lines either side of a bridge say river plainly.

What it does and does not do:

* **One small request per plaza, once.** The answer is kept in ``map_cache``,
  because the roads around a plaza do not change between filings and the
  public Overpass servers ask for light use. A plaza that has been on a filing
  before costs nothing.
* **Never blocks the invoice.** A slow, failing or unreachable service gives no
  roads, the inset is drawn as it was without them, and nothing is cached, so
  the next filing asks again. Misses are fetched in parallel under a deadline.
* **Major roads only.** Motorway, trunk, primary, secondary, tertiary. Side
  streets would turn a 220-pixel square into noise.
* **Attribution.** The data is OpenStreetMap's, under the ODbL, which requires
  saying so on the thing it is used in. The sheet does, whenever roads are drawn.

The response shape is Overpass's ``out geom``::

    {"elements": [{"type": "way", "tags": {"highway": "motorway", "name": ...,
                   "ref": "I 87"}, "geometry": [{"lat": .., "lon": ..}, ...]}]}
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy.orm import Session

from turonomics_api.db.models import MapCache
from turonomics_api.ingest.evidence import INSET_RADIUS_M, Inset, RoadLine, WaterShape
from turonomics_api.ingest.evidence_map import clip_polyline
from turonomics_api.settings import overpass_url

log = logging.getLogger("turonomics.ingest.osm_roads")

# What the public instances ask for: say who you are.
USER_AGENT = "Turonomics/1.0 (+https://github.com/jambola441/turonomics; toll evidence maps)"

# A little wider than the drawn circle, so a road crossing the edge is whole
# when it is clipped to it.
_QUERY_RADIUS_M = int(INSET_RADIUS_M + 100)

QUERY = (
    "[out:json][timeout:20];("
    "way(around:{radius},{lat},{lon})"
    '[highway~"^(motorway|trunk|primary|secondary|tertiary)(_link)?$"];'
    "way(around:{radius},{lat},{lon})[natural=water];"
    "way(around:{radius},{lat},{lon})[waterway=riverbank];"
    "relation(around:{radius},{lat},{lon})[natural=water];"
    "way(around:{radius},{lat},{lon})[natural=coastline];"
    # Cut to the box: a river's outline is hundreds of kilometres long.
    ");out geom({south},{west},{north},{east});"
)

# Tried in turn when one cannot be reached or answers badly. The public
# instances are run by volunteers and one being down is ordinary.
FALLBACK_URLS = (
    "https://overpass.private.coffee/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
)

_KINDS = frozenset({"motorway", "trunk", "primary", "secondary", "tertiary"})

# Roads change slowly; a plaza's surroundings, slower. Refetched after this.
CACHE_TTL = timedelta(days=180)
# Bumped when what is fetched changes, so entries without water are fetched
# again rather than kept for six months.
CACHE_VERSION = "map2"

# How long all the misses together may take before the filing goes ahead without
# them. Each request has its own, shorter, timeout; a fallback instance gets one
# too, so this leaves room for two tries.
DEADLINE_SECONDS = 20.0
REQUEST_TIMEOUT_SECONDS = 8.0


def _post(url: str, **kwargs: Any) -> httpx.Response:
    """One POST; a seam for the tests."""
    return httpx.post(url, **kwargs)


def _box(lat: float, lon: float, radius_m: int) -> dict[str, float]:
    dlat = radius_m / 111_320.0
    dlon = radius_m / (111_320.0 * math.cos(math.radians(lat)))
    return {"south": round(lat - dlat, 6), "west": round(lon - dlon, 6),
            "north": round(lat + dlat, 6), "east": round(lon + dlon, 6)}

Fetcher = Callable[[float, float, int], Mapping[str, Any] | None]


def fetch_overpass(lat: float, lon: float, radius_m: int) -> Mapping[str, Any] | None:
    """The roads and water around a point, or None for any reason they could not be had.

    The configured instance first, then the fallbacks, each once.
    """
    query = QUERY.format(radius=radius_m, lat=lat, lon=lon, **_box(lat, lon, radius_m))
    for url in dict.fromkeys((overpass_url(), *FALLBACK_URLS)):
        try:
            response = _post(
                url,
                data={"data": query},
                headers={"User-Agent": USER_AGENT},
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
            if response.status_code != 200:
                log.warning("overpass %s answered %s for %.5f,%.5f",
                            url, response.status_code, lat, lon)
                continue
            body = response.json()
        except (httpx.HTTPError, ValueError) as error:
            log.warning("overpass %s failed for %.5f,%.5f: %s", url, lat, lon, error)
            continue
        if isinstance(body, dict) and isinstance(body.get("elements"), list):
            return body
    return None


def _pieces(geometry: Sequence[Any]) -> list[list[list[float]]]:
    """A way's points as runs, split where Overpass left out a point cut by the box."""
    runs: list[list[list[float]]] = [[]]
    for node in geometry:
        if (
            isinstance(node, Mapping)
            and isinstance(node.get("lat"), int | float)
            and isinstance(node.get("lon"), int | float)
        ):
            runs[-1].append([round(float(node["lat"]), 6), round(float(node["lon"]), 6)])
        elif runs[-1]:
            runs.append([])
    return [run for run in runs if len(run) >= 2]


def _water_of(geometry: Sequence[Any]) -> list[dict[str, Any]]:
    """An outline whole and closed is an area; anything cut by the box is shore."""
    whole = len(geometry) >= 4 and all(isinstance(n, Mapping) for n in geometry)
    runs = _pieces(geometry)
    if whole and len(runs) == 1 and runs[0][0] == runs[0][-1]:
        return [{"w": "area", "g": runs[0]}]
    return [{"w": "shore", "g": run} for run in runs]


def compact_water(body: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Lakes as areas, and the shores of rivers, bays and the sea as lines."""
    out: list[dict[str, Any]] = []
    for element in body.get("elements") or []:
        if not isinstance(element, Mapping):
            continue
        tags = element.get("tags")
        tags = tags if isinstance(tags, Mapping) else {}
        watery = (tags.get("natural") in ("water", "coastline")
                  or tags.get("waterway") == "riverbank")
        if not watery:
            continue
        if element.get("type") == "way" and isinstance(element.get("geometry"), list):
            out.extend(_water_of(element["geometry"]))
        elif element.get("type") == "relation":
            for member in element.get("members") or []:
                if (isinstance(member, Mapping) and member.get("type") == "way"
                        and member.get("role") in ("outer", "inner", "")
                        and isinstance(member.get("geometry"), list)):
                    out.extend(_water_of(member["geometry"]))
    return out


def compact_ways(body: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Just what the drawing needs: name, kind, and the points. Smaller to keep."""
    out: list[dict[str, Any]] = []
    for element in body.get("elements") or []:
        if not isinstance(element, Mapping) or element.get("type") != "way":
            continue
        tags = element.get("tags")
        geometry = element.get("geometry")
        if not isinstance(tags, Mapping) or not isinstance(geometry, list):
            continue
        highway = tags.get("highway")
        kind = highway.removesuffix("_link") if isinstance(highway, str) else None
        if kind not in _KINDS:
            continue
        points: list[list[float]] = []
        for node in geometry:
            if (
                isinstance(node, Mapping)
                and isinstance(node.get("lat"), int | float)
                and isinstance(node.get("lon"), int | float)
            ):
                points.append([round(float(node["lat"]), 6), round(float(node["lon"]), 6)])
        if len(points) < 2:
            continue
        name = tags.get("name") or tags.get("ref")
        out.append({"n": name if isinstance(name, str) else None, "k": kind, "g": points})
    return out


def road_lines(
    ways: Sequence[Mapping[str, Any]], centre: tuple[float, float], radius_m: float
) -> list[RoadLine]:
    """The compact ways, clipped to the circle, as drawable lines."""
    out: list[RoadLine] = []
    for way in ways:
        points = [(float(p[0]), float(p[1])) for p in way.get("g", [])]
        for piece in clip_polyline(points, centre, radius_m):
            out.append(RoadLine(name=way.get("n"), kind=str(way["k"]), points=piece))
    return out


def _key(inset: Inset) -> str:
    return f"{CACHE_VERSION}:{inset.lat:.5f},{inset.lon:.5f},{_QUERY_RADIUS_M}"


def water_shapes(
    water: Sequence[Mapping[str, Any]], centre: tuple[float, float], radius_m: float
) -> list[WaterShape]:
    """Areas kept whole (the drawing's circle cuts them); shores clipped like roads."""
    out: list[WaterShape] = []
    for shape in water:
        points = [(float(p[0]), float(p[1])) for p in shape.get("g", [])]
        if shape.get("w") == "area":
            out.append(WaterShape(area=True, points=points))
            continue
        for piece in clip_polyline(points, centre, radius_m):
            out.append(WaterShape(area=False, points=piece))
    return out


def add_roads(
    session: Session,
    insets: Sequence[Inset],
    *,
    fetcher: Fetcher | None = None,
    now: datetime | None = None,
) -> list[Inset]:
    """The insets with roads under them where roads could be had.

    Never raises: anything that goes wrong returns the insets as given.
    """
    if not insets:
        return []
    try:
        return _add_roads(session, insets, fetcher or fetch_overpass, now or datetime.now(UTC))
    except Exception:  # noqa: BLE001 - a map is not worth a failed invoice
        log.warning("roads left out of the evidence maps", exc_info=True)
        session.rollback()
        return list(insets)


def _add_roads(
    session: Session, insets: Sequence[Inset], fetch: Fetcher, now: datetime
) -> list[Inset]:
    ways: dict[str, list[dict[str, Any]]] = {}
    water: dict[str, list[dict[str, Any]]] = {}
    misses: dict[str, Inset] = {}
    for inset in insets:
        key = _key(inset)
        cached = session.get(MapCache, key)
        if cached is not None and cached.fetched_at > now - CACHE_TTL:
            stored = cached.data.get("ways")
            ways[key] = stored if isinstance(stored, list) else []
            wet = cached.data.get("water")
            water[key] = wet if isinstance(wet, list) else []
        else:
            misses[key] = inset

    if misses:
        # HTTP only in the threads; the session stays on this one.
        pool = ThreadPoolExecutor(max_workers=min(4, len(misses)))
        futures = {
            pool.submit(fetch, inset.lat, inset.lon, _QUERY_RADIUS_M): key
            for key, inset in misses.items()
        }
        done, _ = wait(futures, timeout=DEADLINE_SECONDS)
        pool.shutdown(wait=False, cancel_futures=True)
        for future in done:
            key = futures[future]
            try:
                body = future.result()
            except Exception:  # noqa: BLE001
                body = None
            if body is None:
                continue  # not cached: the next filing asks again
            compact = compact_ways(body)
            wet = compact_water(body)
            ways[key], water[key] = compact, wet
            session.merge(MapCache(key=key, data={"ways": compact, "water": wet}, fetched_at=now))
        session.commit()

    out: list[Inset] = []
    for inset in insets:
        key = _key(inset)
        found, wet = ways.get(key), water.get(key)
        if not found and not wet:
            out.append(inset)
            continue
        centre = (inset.lat, inset.lon)
        out.append(replace(
            inset,
            roads=road_lines(found or [], centre, INSET_RADIUS_M),
            water=water_shapes(wet or [], centre, INSET_RADIUS_M),
        ))
    return out
