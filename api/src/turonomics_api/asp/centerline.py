"""Kerb lines for cleaning signs the city never placed on a map.

The sign loader traces each block-side's kerb from the signs on it, and finds
them by position. About one Brooklyn broom sign in ten has no position at
all: 3,078 signs on 943 block-sides in October 2026. A box query never sees
them, so those blocks looked like blocks with no rules. St Marks Place
between Fifth and Sixth Avenues, where Jimmy parks, was one: eleven broom
signs, none with coordinates, and the car was offered Prospect Place, 70
metres away, as the nearest kerb.

The signs still name their block (on, from and to streets) and their side. So
the block is found in the city's street centreline dataset instead. The
on-street's centreline pieces are chained from a node it shares with the from
street to one it shares with the to street. The kerb is then that line offset
by half the roadbed width toward the named side.

Names differ between the two datasets ("ST MARKS PLACE" against "ST MARKS
AVE", "5 AVENUE" against "5 AVE"), so streets are compared without their type
word. That alone would confuse the two St Marks, but the chain also has to
join both cross streets, and two different streets do not both do that.

A block whose names do not chain is left out rather than guessed. In the
Jimmy box that is about a dozen block-sides, mostly where Park Avenue runs
under the expressway and at the box's edges.
"""

from __future__ import annotations

import collections
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

import httpx
from geoalchemy2 import Geography
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from turonomics_api.asp.ingest import (
    NY_STATE_PLANE,
    SOCRATA,
    IngestReport,
    _block_key,
    find_block_side,
    save_block_side,
)
from turonomics_api.asp.signs import ParsedRule, try_parse
from turonomics_api.db.models import StreetSegmentSide

log = logging.getLogger("turonomics.asp.centerline")

CENTERLINE = "https://data.cityofnewyork.us/resource/inkn-q76z.json"

# The centreline's roadbed width, used when a piece has none. A typical
# one-lane Brooklyn side street with parking both sides.
DEFAULT_WIDTH_FT = 30.0

# Centreline pieces meet at shared vertices; five decimal places of a degree
# is about a metre, well inside any real gap between two intersections.
_NODE_PLACES = 5

# How many centreline pieces one block may span. Most blocks are one piece; a
# block split by a driveway or a plaza edge is two or three.
_MAX_PIECES = 5

_TYPES = {
    "AVENUE": "AVE", "AVE": "AVE", "STREET": "ST", "ST": "ST", "PLACE": "PL", "PL": "PL",
    "ROAD": "RD", "RD": "RD", "BOULEVARD": "BLVD", "BLVD": "BLVD", "PARKWAY": "PKWY",
    "PKWY": "PKWY", "DRIVE": "DR", "DR": "DR", "COURT": "CT", "CT": "CT", "LANE": "LN",
    "LN": "LN", "TERRACE": "TER", "TER": "TER", "PLAZA": "PLZ", "PLZ": "PLZ",
}
_WORDS = {"EAST": "E", "WEST": "W", "NORTH": "N", "SOUTH": "S", "SAINT": "ST",
          "EXTENSION": "EXT"}


def street_base(name: str) -> str:
    """A street's name without its type word, in one spelling.

    "SAINT MARKS AVENUE", "ST MARKS PLACE" and "ST MARKS AVE" all give
    "ST MARKS". A name that is only a type word ("PLAZA") keeps it.
    """
    words = re.sub(r"\s+", " ", (name or "").upper().replace(".", " ")).strip().split(" ")
    words = [_TYPES.get(w, _WORDS.get(w, w)) for w in words if w]
    if len(words) > 1 and words[-1] in _TYPES.values():
        words = words[:-1]
    return " ".join(words)


@dataclass(frozen=True)
class Piece:
    """One centreline segment: a street between two nodes, in lon/lat."""

    name: str
    physical_id: str
    coords: tuple[tuple[float, float], ...]
    width_ft: float

    @property
    def ends(self) -> tuple[tuple[float, float], tuple[float, float]]:
        return _node(self.coords[0]), _node(self.coords[-1])


def _node(point: tuple[float, float]) -> tuple[float, float]:
    return round(point[0], _NODE_PLACES), round(point[1], _NODE_PLACES)


def pieces_from_rows(rows: list[dict[str, Any]]) -> list[Piece]:
    out: list[Piece] = []
    for row in rows:
        geom = row.get("the_geom") or {}
        lines = geom.get("coordinates") or []
        if geom.get("type") == "LineString":
            lines = [lines]
        coords = tuple((float(c[0]), float(c[1])) for line in lines for c in line)
        if len(coords) < 2 or not row.get("full_street_name"):
            continue
        try:
            width = float(row.get("streetwidth") or 0) or DEFAULT_WIDTH_FT
        except (TypeError, ValueError):
            width = DEFAULT_WIDTH_FT
        out.append(Piece(row["full_street_name"], str(row.get("physicalid") or ""),
                         coords, width))
    return out


class Streets:
    """The centreline pieces of an area, indexed by street name."""

    def __init__(self, pieces: list[Piece]) -> None:
        self.by_base: dict[str, list[Piece]] = collections.defaultdict(list)
        for piece in pieces:
            self.by_base[street_base(piece.name)].append(piece)

    def _nodes(self, name: str) -> set[tuple[float, float]]:
        return {end for piece in self.by_base.get(street_base(name), []) for end in piece.ends}

    def block(self, on: str, frm: str, to: str) -> list[Piece] | None:
        """The pieces of ``on`` running from its crossing with ``frm`` to ``to``.

        Breadth first, so the shortest run wins when a street meets a cross
        street twice (a crescent, or a street on both sides of a divided one).
        """
        pieces = self.by_base.get(street_base(on), [])
        starts, ends = self._nodes(frm), self._nodes(to)
        if not pieces or not starts or not ends:
            return None
        touching: dict[tuple[float, float], list[Piece]] = collections.defaultdict(list)
        for piece in pieces:
            for end in piece.ends:
                touching[end].append(piece)
        for start in sorted(starts & touching.keys()):
            seen = {start}
            queue: collections.deque[tuple[tuple[float, float], list[Piece]]] = (
                collections.deque([(start, [])]))
            while queue:
                node, path = queue.popleft()
                if path and node in ends:
                    return path
                if len(path) >= _MAX_PIECES:
                    continue
                for piece in touching[node]:
                    a, b = piece.ends
                    nxt = b if a == node else a
                    if nxt not in seen:
                        seen.add(nxt)
                        queue.append((nxt, [*path, piece]))
        return None


@dataclass
class PlacementReport:
    block_sides: int = 0
    already_loaded: int = 0
    no_kerb: int = 0
    placed: IngestReport = field(default_factory=IngestReport)

    def summary(self) -> str:
        return (
            f"{self.placed.block_sides_created} block-sides placed from the centreline, "
            f"{self.placed.rules_created} rules; {self.already_loaded} already loaded, "
            f"{self.no_kerb} with no kerb line"
        )


def fetch_unplaced_signs(
    *,
    http: httpx.Client | None = None,
    limit: int = 50000,
    app_token: str | None = None,
) -> list[dict[str, Any]]:
    """Current Brooklyn street-cleaning signs that have no coordinates."""
    where = (
        "record_type='Current' AND borough='Brooklyn' "
        "AND upper(sign_description) like '%BROOM%' "
        "AND sign_x_coord IS NULL"
    )
    headers = {"X-App-Token": app_token} if app_token else {}
    client = http or httpx.Client(timeout=180.0)
    resp = client.get(SOCRATA, params={"$where": where, "$limit": limit}, headers=headers)
    resp.raise_for_status()
    rows: list[dict[str, Any]] = resp.json()
    return rows


def area_polygon(session: Session, bbox: tuple[int, int, int, int]) -> str:
    """A state-plane box as a lon/lat polygon, for the centreline query."""
    x0, y0, x1, y1 = bbox
    wkt = session.scalar(select(func.ST_AsText(func.ST_Transform(
        func.ST_MakeEnvelope(x0, y0, x1, y1, NY_STATE_PLANE), 4326))))
    return str(wkt)


def fetch_centerline(
    polygon_wkt: str,
    *,
    http: httpx.Client | None = None,
    limit: int = 50000,
    app_token: str | None = None,
) -> list[dict[str, Any]]:
    """The street centreline pieces crossing a lon/lat polygon.

    ``intersects`` with a polygon: the dataset answers ``within_circle`` with
    nothing at all.
    """
    params: dict[str, str | int] = {
        "$select": "full_street_name,physicalid,streetwidth,the_geom",
        "$where": f"intersects(the_geom, '{polygon_wkt}')",
        "$limit": limit,
    }
    headers = {"X-App-Token": app_token} if app_token else {}
    client = http or httpx.Client(timeout=180.0)
    resp = client.get(CENTERLINE, params=params, headers=headers)
    resp.raise_for_status()
    rows: list[dict[str, Any]] = resp.json()
    return rows


_OFFSETS = text(
    """
    SELECT ST_AsGeoJSON(a), ST_X(ST_Centroid(a)), ST_Y(ST_Centroid(a)),
           ST_AsGeoJSON(b), ST_X(ST_Centroid(b)), ST_Y(ST_Centroid(b))
    FROM (SELECT ST_OffsetCurve(g, :d) AS a, ST_OffsetCurve(g, -:d) AS b
          FROM (SELECT ST_Transform(ST_GeomFromEWKT(:ewkt), :srid) AS g) AS line) AS offsets
    """
)


def kerb(session: Session, piece: Piece, side_code: str) -> list[tuple[float, float]] | None:
    """One piece's kerb on the named side, in state-plane feet.

    Both offsets are made and the one lying further toward the side is kept,
    which does not depend on which way the piece was drawn. North and south
    compare the offsets' northings, east and west their eastings.
    """
    coords = ", ".join(f"{x} {y}" for x, y in piece.coords)
    row = session.execute(_OFFSETS, {
        "d": piece.width_ft / 2, "ewkt": f"SRID=4326;LINESTRING({coords})",
        "srid": NY_STATE_PLANE,
    }).one()
    a, ax, ay, b, bx, by = row
    if side_code in ("N", "S"):
        first = (ay > by) == (side_code == "N")
    else:
        first = (ax > bx) == (side_code == "E")
    chosen = json.loads(a if first else b)
    if chosen.get("type") != "LineString" or len(chosen["coordinates"]) < 2:
        return None
    return [(float(x), float(y)) for x, y in chosen["coordinates"]]


def _lines_expression(lines: list[list[tuple[float, float]]]) -> ColumnElement[Any]:
    parts = ", ".join("(" + ", ".join(f"{x} {y}" for x, y in line) + ")" for line in lines)
    ewkt = f"SRID={NY_STATE_PLANE};MULTILINESTRING({parts})"
    return func.ST_Transform(func.ST_LineMerge(func.ST_GeomFromEWKT(ewkt)), 4326).cast(Geography)


def load_unplaced(
    session: Session, signs: list[dict[str, Any]], centerline: list[dict[str, Any]]
) -> PlacementReport:
    """Place coordless sign rows on the centreline and load their rules.

    A block-side traced from its signs is left alone: one sign with
    coordinates puts its line closer to the kerb than an offset centreline
    can, and the sign loader already took the rules of every sign on the
    block. One placed here before is placed again, which is how a forced
    reload picks up the dataset's changes to it.
    """
    report = PlacementReport()
    streets = Streets(pieces_from_rows(centerline))
    groups: dict[tuple[str, str, str, str], list[dict[str, Any]]] = collections.defaultdict(list)
    for row in signs:
        key = _block_key(row)
        if key is not None:
            groups[key].append(row)

    for key, rows in groups.items():
        on, frm, to, side_code = key
        pieces = streets.block(on, frm, to)
        if pieces is None:
            # Most of the borough's blocks are outside this area's centreline.
            continue
        report.block_sides += 1
        existing = find_block_side(session, key)
        if existing is not None and existing.nyc_physical_id is None:
            report.already_loaded += 1
            continue
        rules: dict[tuple[tuple[int, ...], Any, Any], ParsedRule] = {}
        for row in rows:
            rule, _why = try_parse(row.get("sign_description") or "")
            if rule is not None:
                rules[(rule.days_of_week, rule.starts_at, rule.ends_at)] = rule
        if not rules:
            continue
        lines = [line for line in (kerb(session, p, side_code) for p in pieces) if line]
        if not lines:
            report.no_kerb += 1
            continue
        save_block_side(session, key, _lines_expression(lines), list(rules.values()),
                        report.placed, physical_id=pieces[0].physical_id or None)

    session.commit()
    log.info("%s", report.summary())
    return report


def placed_count(session: Session) -> int:
    """Block-sides placed from the centreline, which is how a boot knows the
    pass has run."""
    return session.scalar(
        select(func.count()).select_from(StreetSegmentSide)
        .where(StreetSegmentSide.nyc_physical_id.is_not(None))
    ) or 0
