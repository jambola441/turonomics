"""Tests for the roads drawn under the tracker on the evidence maps.

Roads are context, never evidence, so what matters is that they can never make
an invoice worse: a slow or failing service gives no roads and an unchanged
sheet, a failure is not remembered as an answer, and nothing the service sends
can break the picture.
"""

from __future__ import annotations

import re
import time
from datetime import UTC, datetime
from datetime import timedelta as td
from typing import Any
from xml.dom import minidom

import httpx
import pytest

from turonomics_api.db.models import MapCache
from turonomics_api.ingest import osm_roads
from turonomics_api.ingest.evidence import (
    INSET_RADIUS_M,
    EvidenceRow,
    EvidenceSheet,
    Inset,
    RoadLine,
    _road_labels,
    evidence_svg,
)
from turonomics_api.ingest.evidence_map import clip_polyline
from turonomics_api.ingest.osm_roads import (
    CACHE_TTL,
    add_roads,
    compact_ways,
    fetch_overpass,
    road_lines,
)
from turonomics_api.ingest.plazas import locate
from turonomics_api.ingest.trip_map import _metres

from .conftest import requires_db

T0 = datetime(2026, 7, 23, 18, 57, tzinfo=UTC)
VNB = locate("VNB")
assert VNB is not None
AT = (VNB.lat, VNB.lon)
DEG = 1 / 111_320


def _way(highway: str, name: str | None, points: list[tuple[float, float]], **tags: Any) -> dict:
    out_tags: dict[str, Any] = {"highway": highway, **tags}
    if name is not None:
        out_tags["name"] = name
    return {
        "type": "way", "id": 1, "tags": out_tags,
        "geometry": [{"lat": lat, "lon": lon} for lat, lon in points],
    }


def _east_west(length_m: float, offset_m: float = 0.0) -> list[tuple[float, float]]:
    half = length_m / 2 * DEG / 0.757  # the longitude degree is shorter at this latitude
    lat = VNB.lat + offset_m * DEG
    return [(lat, VNB.lon - half), (lat, VNB.lon), (lat, VNB.lon + half)]


BODY = {
    "elements": [
        _way("motorway", "Gowanus Expressway", _east_west(3000, 400)),
        _way("secondary", "Hylan Boulevard", _east_west(3000, -500)),
    ]
}


# ---------------------------------------------------------------------------
# Reading what Overpass sends
# ---------------------------------------------------------------------------
def test_major_roads_are_kept_with_their_names() -> None:
    ways = compact_ways(BODY)
    assert [(w["k"], w["n"]) for w in ways] == [
        ("motorway", "Gowanus Expressway"), ("secondary", "Hylan Boulevard"),
    ]


def test_a_link_road_is_drawn_as_its_parent_kind() -> None:
    [way] = compact_ways({"elements": [_way("motorway_link", "Ramp", _east_west(500))]})
    assert way["k"] == "motorway"


def test_a_road_with_no_name_falls_back_to_its_route_number() -> None:
    [way] = compact_ways({"elements": [_way("trunk", None, _east_west(500), ref="I 278")]})
    assert way["n"] == "I 278"


@pytest.mark.parametrize("highway", ["residential", "footway", "service", "track", "path"])
def test_minor_ways_are_not_drawn(highway: str) -> None:
    assert compact_ways({"elements": [_way(highway, "Alley", _east_west(500))]}) == []


def test_malformed_elements_are_skipped_not_fatal() -> None:
    body = {
        "elements": [
            "not a dict",
            {"type": "node", "id": 2},
            {"type": "way", "tags": {"highway": "primary"}},
            {"type": "way", "geometry": [{"lat": 1, "lon": 2}, {"lat": 1, "lon": 3}]},
            _way("primary", "One Point", [AT]),
            {"type": "way", "tags": {"highway": "primary", "name": "Bad"},
             "geometry": [{"lat": "x", "lon": 1}, {"lat": 2, "lon": 3}]},
            _way("primary", "Fine", _east_west(500)),
        ]
    }
    assert [w["n"] for w in compact_ways(body)] == ["Fine"]


def test_nothing_beyond_the_circle_is_kept_when_roads_are_clipped() -> None:
    lines = road_lines(compact_ways(BODY), AT, INSET_RADIUS_M)
    assert lines
    for line in lines:
        for point in line.points:
            assert _metres(point, AT) <= INSET_RADIUS_M + 2


def test_a_road_wholly_outside_the_circle_gives_nothing() -> None:
    far = {"elements": [_way("primary", "Far", _east_west(2000, 5000))]}
    assert road_lines(compact_ways(far), AT, INSET_RADIUS_M) == []


def test_clip_polyline_keeps_a_road_that_crosses_with_both_ends_outside() -> None:
    pieces = clip_polyline([_east_west(6000)[0], _east_west(6000)[2]], AT, INSET_RADIUS_M)
    assert len(pieces) == 1


# ---------------------------------------------------------------------------
# Asking
# ---------------------------------------------------------------------------
class _Response:
    def __init__(self, status: int, payload: Any = None, bad_json: bool = False) -> None:
        self.status_code = status
        self._payload, self._bad = payload, bad_json

    def json(self) -> Any:
        if self._bad:
            raise ValueError("not json")
        return self._payload


def test_a_good_answer_is_returned_and_the_asker_identifies_itself(monkeypatch) -> None:
    seen: dict[str, Any] = {}

    def post(url, **kwargs):
        seen.update(url=url, **kwargs)
        return _Response(200, BODY)

    monkeypatch.setattr(httpx, "post", post)
    monkeypatch.setenv("OVERPASS_URL", "https://overpass.example/api")
    assert fetch_overpass(*AT, 1300) == BODY
    assert seen["url"] == "https://overpass.example/api"
    assert "Turonomics" in seen["headers"]["User-Agent"]
    assert 'around:1300' in seen["data"]["data"]


@pytest.mark.parametrize(
    "reply",
    [
        _Response(500),
        _Response(429),
        _Response(200, bad_json=True),
        _Response(200, {"no": "elements"}),
        _Response(200, ["a list"]),
    ],
)
def test_anything_but_a_good_answer_is_none(monkeypatch, reply) -> None:
    monkeypatch.setattr(httpx, "post", lambda *a, **k: reply)
    assert fetch_overpass(*AT, 1300) is None


def test_a_service_that_cannot_be_reached_is_none_not_an_error(monkeypatch) -> None:
    def boom(*a, **k):
        raise httpx.ConnectTimeout("slow")

    monkeypatch.setattr(httpx, "post", boom)
    assert fetch_overpass(*AT, 1300) is None


# ---------------------------------------------------------------------------
# Drawing them
# ---------------------------------------------------------------------------
def _inset(roads=(), **over) -> Inset:
    base: dict[str, Any] = dict(
        plaza="VNB", name="Verrazzano-Narrows Bridge", lat=VNB.lat, lon=VNB.lon,
        segments=[[(VNB.lat + 0.0004, VNB.lon - 0.01), (VNB.lat + 0.0004, VNB.lon + 0.01)]],
        closest_m=40, times=[T0], roads=list(roads),
    )
    base.update(over)
    return Inset(**base)


def _sheet(*insets: Inset) -> EvidenceSheet:
    return EvidenceSheet(
        reservation_id="59007349", guest_name="Erica", vehicle="Jimmy", plate="LEH9892",
        starts_at=T0 - td(days=1), ends_at=T0 + td(days=3),
        rows=[EvidenceRow(occurred_at=T0, plaza="VNB", amount_cents=746)],
        insets=list(insets), track_source="bouncie",
    )


def _lines() -> list[RoadLine]:
    return road_lines(compact_ways(BODY), AT, INSET_RADIUS_M)


def test_a_sheet_without_roads_has_no_credit_and_no_road_styling() -> None:
    svg = evidence_svg(_sheet(_inset()))
    assert "OpenStreetMap" not in svg
    assert "#94a3b8" not in svg


def test_roads_are_drawn_and_credited() -> None:
    svg = evidence_svg(_sheet(_inset(_lines())))
    minidom.parseString(svg)
    assert "Roads: © OpenStreetMap contributors" in svg, "the licence requires saying so"
    assert "#94a3b8" in svg, "the motorway"
    assert "#b8c2d0" in svg, "the secondary road"


def test_heavier_roads_are_drawn_over_lighter_ones() -> None:
    svg = evidence_svg(_sheet(_inset(_lines())))
    assert svg.index("#b8c2d0") < svg.index("#94a3b8")


def test_the_track_is_drawn_over_the_roads_and_names_over_the_track() -> None:
    svg = evidence_svg(_sheet(_inset(_lines())))
    assert svg.index("#94a3b8") < svg.index('stroke="#2563eb"')
    assert svg.index('stroke="#2563eb"') < svg.index("paint-order")


def test_everything_in_an_inset_is_clipped_to_its_circle() -> None:
    """So a label can never run out over the caption or the next inset."""
    svg = evidence_svg(_sheet(_inset(_lines()), _inset(_lines())))
    assert svg.count("<clipPath") == 2
    ids = re.findall(r'<clipPath id="(inset-clip-\d+)"', svg)
    assert len(set(ids)) == 2, "one id per inset, or the second clips to the first"
    assert all(f'clip-path="url(#{i})"' in svg for i in ids)


def test_a_road_name_is_shortened_and_laid_along_the_road() -> None:
    svg = evidence_svg(_sheet(_inset(_lines())))
    assert "Gowanus Expwy" in svg and "Gowanus Expressway" not in svg


def test_a_name_with_an_ampersand_does_not_break_the_image() -> None:
    roads = road_lines(compact_ways({"elements": [_way("primary", "Smith & Sons <Rd>", _east_west(3000, 400))]}), AT, INSET_RADIUS_M)
    svg = evidence_svg(_sheet(_inset(roads)))
    minidom.parseString(svg)
    assert "Smith &amp; Sons" in svg


def _labels(roads: list[RoadLine]):
    scale = (min(340, 190) / 2 - 6) / INSET_RADIUS_M
    return _road_labels(roads, AT, scale, 170, 95)


def test_at_most_three_roads_are_named() -> None:
    body = {"elements": [_way("primary", f"Road {i}", _east_west(3000, 150 * i + 200)) for i in range(6)]}
    assert len(_labels(road_lines(compact_ways(body), AT, INSET_RADIUS_M))) == 3


def test_a_name_that_does_not_fit_along_its_road_is_left_off() -> None:
    """A label longer than the road it names, or over a stub, is worse than none."""
    stub = {"elements": [_way("primary", "A Very Long Avenue Name Indeed", _east_west(500, 300))]}
    assert _labels(road_lines(compact_ways(stub), AT, INSET_RADIUS_M)) == []


def test_a_label_does_not_sit_on_the_plaza_marker() -> None:
    through_the_plaza = {"elements": [_way("primary", "Marker Road", _east_west(3000, 0))]}
    assert _labels(road_lines(compact_ways(through_the_plaza), AT, INSET_RADIUS_M)) == []


def test_labels_are_never_upside_down() -> None:
    for angle_name, pts in (
        ("East West", _east_west(3000, 400)),
        ("West East", list(reversed(_east_west(3000, -400)))),
    ):
        body = {"elements": [_way("primary", angle_name, pts)]}
        for _, _, _, angle in _labels(road_lines(compact_ways(body), AT, INSET_RADIUS_M)):
            assert -90 <= angle <= 90


def test_a_road_cut_into_blocks_is_still_named() -> None:
    """OpenStreetMap cuts a road into a way per block; the name has to fit
    along the whole of it, not along one block."""
    pts = _east_west(3000, 400)
    pieces = [pts[:2], pts[1:]]
    body = {"elements": [_way("primary", "Atlantic Avenue", p) for p in pieces]}
    assert [t for t, *_ in _labels(road_lines(compact_ways(body), AT, INSET_RADIUS_M))] == ["Atlantic Ave"]


def test_a_road_of_many_tiny_pieces_is_not_named_from_one_of_them() -> None:
    """Long enough in all, but the label would hang from a piece a fraction of
    its own length, and read as belonging to the wrong stretch."""
    points = _east_west(2800, 400)
    start, end = points[0], points[2]
    n = 16
    chain = [(start[0], start[1] + (end[1] - start[1]) * i / n) for i in range(n + 1)]
    pieces = [chain[i : i + 2] for i in range(n)]
    body = {"elements": [_way("primary", "Atlantic Avenue", piece) for piece in pieces]}
    assert _labels(road_lines(compact_ways(body), AT, INSET_RADIUS_M)) == []


# ---------------------------------------------------------------------------
# Fetching once, and never at the invoice's expense
# ---------------------------------------------------------------------------
class _Counting:
    def __init__(self, body=BODY, delay: float = 0.0) -> None:
        self.calls: list[tuple[float, float]] = []
        self.body, self.delay = body, delay

    def __call__(self, lat: float, lon: float, radius: int):
        self.calls.append((lat, lon))
        time.sleep(self.delay)
        return self.body


@requires_db
def test_roads_are_fetched_once_per_plaza_and_then_remembered(session) -> None:
    fetch = _Counting()
    first = add_roads(session, [_inset()], fetcher=fetch, now=T0)
    assert first[0].roads and len(fetch.calls) == 1

    second = add_roads(session, [_inset()], fetcher=fetch, now=T0 + td(days=30))
    assert second[0].roads, "from the cache"
    assert len(fetch.calls) == 1, "and not asked again"


@requires_db
def test_a_cached_answer_expires(session) -> None:
    fetch = _Counting()
    add_roads(session, [_inset()], fetcher=fetch, now=T0)
    add_roads(session, [_inset()], fetcher=fetch, now=T0 + CACHE_TTL + td(days=1))
    assert len(fetch.calls) == 2


@requires_db
def test_a_failed_fetch_gives_no_roads_and_is_not_remembered(session) -> None:
    """A service that was down must be asked again, not trusted as 'no roads'."""
    down = _Counting(body=None)
    plain = _inset()
    assert add_roads(session, [plain], fetcher=down, now=T0) == [plain]
    assert session.get(MapCache, osm_roads._key(plain)) is None

    up = _Counting()
    assert add_roads(session, [plain], fetcher=up, now=T0 + td(minutes=5))[0].roads
    assert len(up.calls) == 1


@requires_db
def test_a_fetcher_that_raises_changes_nothing(session) -> None:
    def boom(*args):
        raise RuntimeError("Overpass is on fire")

    plain = _inset()
    assert add_roads(session, [plain], fetcher=boom, now=T0) == [plain]


@requires_db
def test_each_plaza_is_fetched_separately(session) -> None:
    hct = locate("HCT")
    assert hct is not None
    fetch = _Counting()
    out = add_roads(session, [_inset(), _inset(plaza="HCT", lat=hct.lat, lon=hct.lon)], fetcher=fetch, now=T0)
    assert len(fetch.calls) == 2
    assert len(out) == 2


@requires_db
def test_a_slow_service_does_not_hold_the_invoice_up(session, monkeypatch) -> None:
    monkeypatch.setattr(osm_roads, "DEADLINE_SECONDS", 0.2)
    slow = _Counting(delay=1.5)
    plain = _inset()
    started = time.monotonic()
    out = add_roads(session, [plain], fetcher=slow, now=T0)
    assert time.monotonic() - started < 1.0
    assert out == [plain], "no roads, and the inset is as it was"


@requires_db
def test_nothing_to_draw_is_not_a_fetch(session) -> None:
    fetch = _Counting()
    assert add_roads(session, [], fetcher=fetch, now=T0) == []
    assert fetch.calls == []


@requires_db
def test_the_cache_key_is_the_plaza_not_the_filing(session) -> None:
    fetch = _Counting()
    add_roads(session, [_inset(times=[T0])], fetcher=fetch, now=T0)
    add_roads(session, [_inset(times=[T0 + td(days=40)])], fetcher=fetch, now=T0)
    assert len(fetch.calls) == 1
