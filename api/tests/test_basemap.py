"""Map tiles under the evidence insets.

The sheets went to guests with a bare track when the road service stopped
answering. What matters here: the tiles land where the plaza is, a sheet with a
missing tile has no map rather than a hole, nothing fetched is fetched twice,
the provider's key is never stored, and nothing about tiles can fail a draft.
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime
from datetime import timedelta as td
from typing import Any
from xml.dom import minidom

import pytest

from turonomics_api.db.models import MapCache
from turonomics_api.ingest.basemap import add_basemap, tile_bounds, tiles_for, zoom_for
from turonomics_api.ingest.evidence import (
    INSET_RADIUS_M,
    EvidenceRow,
    EvidenceSheet,
    Inset,
    RoadLine,
    evidence_svg,
)
from turonomics_api.ingest.plazas import locate

from .conftest import requires_db

T0 = datetime(2026, 10, 20, 18, 0, tzinfo=UTC)
HCT = locate("HCT")
assert HCT is not None
PNG = b"\x89PNG\r\n\x1a\nfake"


def _inset(**over: Any) -> Inset:
    base: dict[str, Any] = dict(
        plaza="HCT", name=HCT.name, lat=HCT.lat, lon=HCT.lon,
        segments=[[(HCT.lat - 0.004, HCT.lon), (HCT.lat + 0.004, HCT.lon)]],
        closest_m=30, times=[T0],
    )
    base.update(over)
    return Inset(**base)


def _sheet(*insets: Inset) -> EvidenceSheet:
    return EvidenceSheet(
        reservation_id="59000000", guest_name="Erica", vehicle="Jimmy", plate="LEH9892",
        starts_at=T0 - td(days=1), ends_at=T0 + td(days=1),
        rows=[EvidenceRow(occurred_at=T0, plaza="HCT", amount_cents=1700)],
        insets=list(insets), track_source="bouncie",
    )


class _Tiles:
    def __init__(self, missing: int = 0) -> None:
        self.urls: list[str] = []
        self.missing = missing

    def __call__(self, url: str) -> bytes | None:
        self.urls.append(url)
        return None if len(self.urls) <= self.missing else PNG


@pytest.fixture(autouse=True)
def _keyed_tiles(monkeypatch):
    monkeypatch.setenv("THUNDERFOREST_API_KEY", "SECRETKEY")


def test_tiles_are_drawn_near_one_to_one_in_new_york() -> None:
    assert zoom_for(HCT.lat) == 13


def test_the_tiles_cover_the_whole_circle() -> None:
    z = zoom_for(HCT.lat)
    tiles = tiles_for(HCT.lat, HCT.lon, INSET_RADIUS_M, z)
    north = max(tile_bounds(*t)[0] for t in tiles)
    west = min(tile_bounds(*t)[1] for t in tiles)
    south = min(tile_bounds(*t)[2] for t in tiles)
    east = max(tile_bounds(*t)[3] for t in tiles)
    reach = INSET_RADIUS_M / 111_320
    assert north > HCT.lat + reach and south < HCT.lat - reach
    assert west < HCT.lon - reach and east > HCT.lon + reach
    assert 1 <= len(tiles) <= 4


@requires_db
def test_an_inset_gets_its_tiles_and_the_providers_credit(session) -> None:
    fetch = _Tiles()
    [inset] = add_basemap(session, [_inset()], fetcher=fetch, now=T0)
    needed = tiles_for(HCT.lat, HCT.lon, INSET_RADIUS_M, zoom_for(HCT.lat))
    assert len(inset.basemap) == len(needed) == len(fetch.urls)
    assert inset.basemap[0].image == "data:image/png;base64," + base64.b64encode(PNG).decode()
    assert inset.basemap_credit and "Thunderforest" in inset.basemap_credit
    assert all("apikey=SECRETKEY" in url for url in fetch.urls), "the key is used to fetch"


@requires_db
def test_the_key_is_never_stored(session) -> None:
    add_basemap(session, [_inset()], fetcher=_Tiles(), now=T0)
    keys = [row.key for row in session.query(MapCache)]
    assert keys and not any("SECRETKEY" in key for key in keys)


@requires_db
def test_tiles_are_fetched_once_and_then_remembered(session) -> None:
    fetch = _Tiles()
    add_basemap(session, [_inset()], fetcher=fetch, now=T0)
    first = len(fetch.urls)
    [again] = add_basemap(session, [_inset()], fetcher=fetch, now=T0 + td(days=30))
    assert again.basemap and len(fetch.urls) == first


@requires_db
def test_a_missing_tile_means_no_map_not_a_hole(session) -> None:
    [inset] = add_basemap(session, [_inset()], fetcher=_Tiles(missing=1), now=T0)
    assert inset.basemap == [] and inset.basemap_credit is None


@requires_db
def test_a_failing_tile_service_changes_nothing(session) -> None:
    def boom(url):
        raise RuntimeError("tiles are down")

    plain = _inset()
    [inset] = add_basemap(session, [plain], fetcher=boom, now=T0)
    assert inset.basemap == []


def test_the_map_is_drawn_under_the_track_in_place_of_the_drawn_roads() -> None:
    from turonomics_api.ingest.evidence import BaseTile

    z = zoom_for(HCT.lat)
    tiles = [BaseTile(*tile_bounds(*t), image="data:image/png;base64,AAAA")
             for t in tiles_for(HCT.lat, HCT.lon, INSET_RADIUS_M, z)]
    road = RoadLine(name="West Street", kind="motorway",
                    points=[(HCT.lat - 0.01, HCT.lon), (HCT.lat + 0.01, HCT.lon)])
    svg = evidence_svg(_sheet(_inset(basemap=tiles, basemap_credit="Maps © Thunderforest",
                                     roads=[road])))
    minidom.parseString(svg)
    assert svg.count("<image ") == len(tiles)
    assert svg.index("<image ") < svg.index('stroke="#2563eb"'), "the track over the map"
    assert "#94a3b8" not in svg and "West Street" not in svg, "the tiles carry the roads"
    assert "Map: Maps © Thunderforest." in svg


def test_without_tiles_the_drawn_roads_are_as_before() -> None:
    road = RoadLine(name="West Street", kind="motorway",
                    points=[(HCT.lat - 0.01, HCT.lon), (HCT.lat + 0.01, HCT.lon)])
    svg = evidence_svg(_sheet(_inset(roads=[road])))
    assert "<image " not in svg and "#94a3b8" in svg


@requires_db
def test_only_a_draft_that_asks_for_tiles_gets_them(api_client, session, monkeypatch) -> None:
    """An extension older than 1.22.2 cannot encode the larger sheet."""
    asked: list[bool] = []

    def spy(session, insets, **kwargs):
        asked.append(True)
        return list(insets)

    monkeypatch.setattr("turonomics_api.routers.invoices.add_basemap", spy)
    from turonomics_api.routers.invoices import _location_insets

    monkeypatch.setattr("turonomics_api.routers.invoices.build_insets",
                        lambda route, crossings: ([_inset()], 0))
    monkeypatch.setattr("turonomics_api.routers.invoices.trip_route", lambda s, t: type("R", (), {"source": "bouncie"})())

    class Line:
        toll_id = None
        plaza = "HCT"
        occurred_at = T0

    trip = object()
    _location_insets(session, trip, [Line()])  # type: ignore[arg-type]
    assert asked == []
    _location_insets(session, trip, [Line()], basemap=True)  # type: ignore[arg-type]
    assert asked == [True]
