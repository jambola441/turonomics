"""Roads around a plaza, from OpenStreetMap, for the evidence maps.

A track on a blank square says very little: nothing shows that a plaza is on a
bridge approach, or that a line 600 m away is the highway alongside it. This
fetches the major roads inside each inset's circle so the sheet can draw them
faintly beneath the track, named.

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
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy.orm import Session

from turonomics_api.db.models import MapCache
from turonomics_api.ingest.evidence import INSET_RADIUS_M, Inset, RoadLine
from turonomics_api.ingest.evidence_map import clip_polyline
from turonomics_api.settings import overpass_url

log = logging.getLogger("turonomics.ingest.osm_roads")

# What the public instances ask for: say who you are.
USER_AGENT = "Turonomics/1.0 (+https://github.com/jambola441/turonomics; toll evidence maps)"

# A little wider than the drawn circle, so a road crossing the edge is whole
# when it is clipped to it.
_QUERY_RADIUS_M = int(INSET_RADIUS_M + 100)

QUERY = (
    "[out:json][timeout:20];"
    "way(around:{radius},{lat},{lon})"
    '[highway~"^(motorway|trunk|primary|secondary|tertiary)(_link)?$"];'
    "out geom;"
)

_KINDS = frozenset({"motorway", "trunk", "primary", "secondary", "tertiary"})

# Roads change slowly; a plaza's surroundings, slower. Refetched after this.
CACHE_TTL = timedelta(days=180)

# How long all the misses together may take before the filing goes ahead without
# them. Each request has its own, shorter, timeout.
DEADLINE_SECONDS = 12.0
REQUEST_TIMEOUT_SECONDS = 8.0

Fetcher = Callable[[float, float, int], Mapping[str, Any] | None]


def fetch_overpass(lat: float, lon: float, radius_m: int) -> Mapping[str, Any] | None:
    """The roads around a point, or None for any reason it could not be had."""
    try:
        response = httpx.post(
            overpass_url(),
            data={"data": QUERY.format(radius=radius_m, lat=lat, lon=lon)},
            headers={"User-Agent": USER_AGENT},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        if response.status_code != 200:
            log.warning("overpass answered %s for %.5f,%.5f", response.status_code, lat, lon)
            return None
        body = response.json()
    except (httpx.HTTPError, ValueError) as error:
        log.warning("overpass fetch failed for %.5f,%.5f: %s", lat, lon, error)
        return None
    return body if isinstance(body, dict) and isinstance(body.get("elements"), list) else None


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
    return f"roads:{inset.lat:.5f},{inset.lon:.5f},{_QUERY_RADIUS_M}"


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
    misses: dict[str, Inset] = {}
    for inset in insets:
        key = _key(inset)
        cached = session.get(MapCache, key)
        if cached is not None and cached.fetched_at > now - CACHE_TTL:
            stored = cached.data.get("ways")
            ways[key] = stored if isinstance(stored, list) else []
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
            ways[key] = compact
            session.merge(MapCache(key=key, data={"ways": compact}, fetched_at=now))
        session.commit()

    out: list[Inset] = []
    for inset in insets:
        found = ways.get(_key(inset))
        if not found:
            out.append(inset)
            continue
        roads = road_lines(found, (inset.lat, inset.lon), INSET_RADIUS_M)
        out.append(replace(inset, roads=roads))
    return out
