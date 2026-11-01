"""A map under each evidence inset: the tiles the fleet page draws, embedded.

The insets drew the tracker on a blank square, with roads and water from
Overpass when it answered. From the API's host it stopped answering: every
fetch failed, and the sheets went to guests with a bare blue line. These are
the raster tiles the fleet page already uses (Thunderforest with the
operator's key, else OpenStreetMap's), which carry water, roads and their
names, fetched by the API and embedded in the sheet as images, so a guest sees
a bridge as a road over a river.

What it will and will not do, as for the roads:

* **The tiles show the place, nothing of the guest's.** Only the plaza's
  surroundings are fetched; the track is drawn over them as before.
* **One fetch per tile, once.** Kept in ``map_cache``: the tiles around a plaza
  do not change between filings, and a plaza filed before costs nothing.
* **Never blocks the invoice.** A tile that cannot be had leaves its inset as
  it would have been without a map, and is asked for again next time.
* **Attribution** is the tile provider's own line, on the sheet whenever a
  tile is drawn.
* **The key never leaves the API.** The cache is keyed by style and tile, not
  by the address, which carries it.
"""

from __future__ import annotations

import base64
import logging
import math
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from html import unescape

import httpx
from sqlalchemy.orm import Session

from turonomics_api.db.models import MapCache
from turonomics_api.ingest.evidence import INSET_RADIUS_M, BaseTile, Inset, inset_metres_per_pixel
from turonomics_api.settings import map_tiles

log = logging.getLogger("turonomics.ingest.basemap")

USER_AGENT = "Turonomics/1.0 (+https://github.com/jambola441/turonomics; toll evidence maps)"
TILE_PX = 256
CACHE_TTL = timedelta(days=180)
DEADLINE_SECONDS = 15.0
REQUEST_TIMEOUT_SECONDS = 8.0
# Web Mercator's metres per pixel at the equator, zoom 0.
_EQUATOR_M_PER_PX = 156_543.034

TileFetcher = Callable[[str], bytes | None]


def zoom_for(lat: float) -> int:
    """The zoom whose pixels are nearest the inset's, so tiles are drawn near 1:1."""
    ground = _EQUATOR_M_PER_PX * math.cos(math.radians(lat))
    return max(1, min(18, round(math.log2(ground / inset_metres_per_pixel()))))


def _tile_xy(lat: float, lon: float, z: int) -> tuple[float, float]:
    n = 2**z
    x = (lon + 180.0) / 360.0 * n
    rad = math.radians(lat)
    y = (1.0 - math.asinh(math.tan(rad)) / math.pi) / 2.0 * n
    return x, y


def tile_bounds(z: int, x: int, y: int) -> tuple[float, float, float, float]:
    """(north, west, south, east) of a tile, in degrees."""
    n = 2**z

    def lat(row: int) -> float:
        return math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * row / n))))

    return lat(y), x / n * 360.0 - 180.0, lat(y + 1), (x + 1) / n * 360.0 - 180.0


def tiles_for(lat: float, lon: float, radius_m: float, z: int) -> list[tuple[int, int, int]]:
    """Every tile the circle around a point touches."""
    dlat = radius_m / 111_320.0
    dlon = radius_m / (111_320.0 * math.cos(math.radians(lat)))
    x0, y0 = _tile_xy(lat + dlat, lon - dlon, z)
    x1, y1 = _tile_xy(lat - dlat, lon + dlon, z)
    return [(z, x, y) for x in range(int(x0), int(x1) + 1) for y in range(int(y0), int(y1) + 1)]


def _style_key(template: str) -> str:
    """The template without its query string, which is where the key is."""
    return template.split("?", 1)[0]


def fetch_tile(url: str) -> bytes | None:
    try:
        response = httpx.get(url, headers={"User-Agent": USER_AGENT},
                             timeout=REQUEST_TIMEOUT_SECONDS)
    except httpx.HTTPError as error:
        log.warning("basemap tile failed: %s", error)
        return None
    if response.status_code != 200 or not response.headers.get("content-type", "").startswith(
        "image/"
    ):
        log.warning("basemap tile answered %s", response.status_code)
        return None
    return response.content


def add_basemap(
    session: Session,
    insets: Sequence[Inset],
    *,
    fetcher: TileFetcher | None = None,
    now: datetime | None = None,
) -> list[Inset]:
    """The insets with map tiles under them where they could be had. Never raises."""
    if not insets:
        return []
    try:
        return _add_basemap(session, insets, fetcher or fetch_tile, now or datetime.now(UTC))
    except Exception:  # noqa: BLE001 - a map is not worth a failed invoice
        log.warning("basemap left out of the evidence maps", exc_info=True)
        session.rollback()
        return list(insets)


def _add_basemap(
    session: Session, insets: Sequence[Inset], fetch: TileFetcher, now: datetime
) -> list[Inset]:
    tiles = map_tiles()
    template = str(tiles["url"])
    credit = unescape(str(tiles["attribution"]))
    style = _style_key(template)

    wanted: dict[tuple[int, int, int], str] = {}
    for inset in insets:
        for tile in tiles_for(inset.lat, inset.lon, INSET_RADIUS_M, zoom_for(inset.lat)):
            wanted[tile] = f"tile:{style}:{tile[0]}/{tile[1]}/{tile[2]}"

    have: dict[tuple[int, int, int], str] = {}
    misses: dict[tuple[int, int, int], str] = {}
    for tile, key in wanted.items():
        cached = session.get(MapCache, key)
        image = cached.data.get("image") if cached is not None else None
        if cached is not None and cached.fetched_at > now - CACHE_TTL and isinstance(image, str):
            have[tile] = image
        else:
            misses[tile] = key

    if misses:
        pool = ThreadPoolExecutor(max_workers=min(6, len(misses)))
        futures = {
            pool.submit(fetch, template.format(z=t[0], x=t[1], y=t[2], s="a")): t
            for t in misses
        }
        done, _ = wait(futures, timeout=DEADLINE_SECONDS)
        pool.shutdown(wait=False, cancel_futures=True)
        for future in done:
            tile = futures[future]
            try:
                body = future.result()
            except Exception:  # noqa: BLE001
                body = None
            if not body:
                continue  # not cached: the next filing asks again
            image = "data:image/png;base64," + base64.b64encode(body).decode("ascii")
            have[tile] = image
            session.merge(MapCache(key=misses[tile], data={"image": image}, fetched_at=now))
        session.commit()

    out: list[Inset] = []
    for inset in insets:
        drawn: list[BaseTile] = []
        needed = tiles_for(inset.lat, inset.lon, INSET_RADIUS_M, zoom_for(inset.lat))
        # All or nothing: a map with a missing square in it is worse than none.
        if needed and all(tile in have for tile in needed):
            for tile in needed:
                north, west, south, east = tile_bounds(*tile)
                drawn.append(BaseTile(north=north, west=west, south=south, east=east,
                                      image=have[tile]))
        out.append(replace(inset, basemap=drawn, basemap_credit=credit if drawn else None))
    return out
