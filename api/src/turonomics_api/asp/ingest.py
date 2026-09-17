"""Load NYC parking signs into street segment sides and cleaning rules.

Geometry comes from the signs themselves rather than from a separate street
centreline dataset. Signs are mounted on the kerb, so the ones on a block-side
trace that kerb directly — which is the line a parked car needs to be measured
against, and is not the same as the street centreline. ``distance_from_intersection``
orders them along the block, so no spatial sorting is needed.

Three properties of the source worth keeping in mind:

* ``sign_x_coord``/``sign_y_coord`` are documented as longitude and latitude
  but are NY State Plane Long Island feet (EPSG:2263). They are transformed in
  the database, where the projection definitions already live.
* ``side_of_street`` is supplied, so a block-side is identified rather than
  inferred.
* A block-side can legitimately carry more than one cleaning rule, covering
  different stretches of a long block. Both are attached and the earliest
  deadline wins, which over-warns on part of such a block rather than missing a
  sweeper. In 11238 this affects about 1% of block-sides.
"""

from __future__ import annotations

import collections
import logging
from dataclasses import dataclass, field
from typing import Any

import httpx
from geoalchemy2 import Geography
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from turonomics_api.asp.signs import ParsedRule, try_parse
from turonomics_api.db.models import AspRule, RuleSource, StreetSegmentSide, StreetSide

log = logging.getLogger("turonomics.asp.ingest")

SOCRATA = "https://data.cityofnewyork.us/resource/nfid-uabd.json"
NY_STATE_PLANE = 2263

_SIDE_CODES = {
    "N": StreetSide.north,
    "S": StreetSide.south,
    "E": StreetSide.east,
    "W": StreetSide.west,
}

# Prospect Heights and its immediate surroundings, in state plane feet.
# Deliberately a little larger than the zip: a car parked a block over the line
# still needs its rules.
ZIP_11238_BBOX = (991558, 180860, 997338, 190790)


@dataclass
class IngestReport:
    signs_seen: int = 0
    signs_unparsed: int = 0
    block_sides: int = 0
    block_sides_created: int = 0
    rules_created: int = 0
    point_only_sides: int = 0
    multi_rule_sides: int = 0
    unparsed_reasons: collections.Counter[str] = field(default_factory=collections.Counter)

    def summary(self) -> str:
        return (
            f"{self.signs_seen} signs -> {self.block_sides} block-sides "
            f"({self.block_sides_created} new), {self.rules_created} rules; "
            f"{self.signs_unparsed} unparsed, {self.point_only_sides} point-only, "
            f"{self.multi_rule_sides} with more than one rule"
        )


def fetch_signs(
    bbox: tuple[int, int, int, int] = ZIP_11238_BBOX,
    *,
    http: httpx.Client | None = None,
    limit: int = 50000,
    app_token: str | None = None,
) -> list[dict[str, Any]]:
    """Current street-cleaning signs inside a state-plane bounding box."""
    x0, y0, x1, y1 = bbox
    where = (
        "record_type='Current' "
        "AND upper(sign_description) like '%BROOM%' "
        f"AND sign_x_coord between {x0} and {x1} "
        f"AND sign_y_coord between {y0} and {y1}"
    )
    headers = {"X-App-Token": app_token} if app_token else {}
    client = http or httpx.Client(timeout=180.0)
    resp = client.get(SOCRATA, params={"$where": where, "$limit": limit}, headers=headers)
    resp.raise_for_status()
    rows: list[dict[str, Any]] = resp.json()
    return rows


def _block_key(row: dict[str, Any]) -> tuple[str, str, str, str] | None:
    on = (row.get("on_street") or "").strip().upper()
    frm = (row.get("from_street") or "").strip().upper()
    to = (row.get("to_street") or "").strip().upper()
    side = (row.get("side_of_street") or "").strip().upper()
    if not on or side not in _SIDE_CODES:
        return None
    return on, frm, to, side


# How far to extend the kerb line past the last sign, in feet. Signs stop short
# of the far intersection, so without this a car parked near the corner matches
# nothing. Over-extending is safe: the neighbouring block has its own line and
# nearest-wins arbitrates. Under-extending is not — it looks like "no rules
# here", which reads as "nothing due".
FORWARD_EXTENSION_FT = 165.0  # ~50 m


def _extend(
    points: list[tuple[float, float]], first_distance_ft: float
) -> list[tuple[float, float]]:
    """Stretch the traced line to cover the whole block.

    The near end is exact: the first sign records its own distance from the
    intersection, so the line extends back by precisely that. The far end has
    no such measurement and uses a constant.

    Coordinates are state plane feet, which is planar, so linear extrapolation
    is correct here in a way it would not be on lon/lat.
    """
    if len(points) < 2:
        return points

    def step(
        a: tuple[float, float], b: tuple[float, float], distance: float
    ) -> tuple[float, float]:
        dx, dy = b[0] - a[0], b[1] - a[1]
        length = (dx * dx + dy * dy) ** 0.5
        if length == 0:
            return a
        return (a[0] - dx / length * distance, a[1] - dy / length * distance)

    out = list(points)
    if first_distance_ft > 0:
        out.insert(0, step(points[0], points[1], first_distance_ft))
    out.append(step(points[-1], points[-2], FORWARD_EXTENSION_FT))
    return out


def _geom_expression(points: list[tuple[float, float]]) -> ColumnElement[Any]:
    """The kerb geometry, projected to lon/lat.

    The transform happens in PostGIS rather than in Python: the projection
    definitions already live there, and a geography column accepts lon/lat
    only, so state-plane coordinates cannot simply be handed over.

    A single sign gives a point rather than a line. Inventing a line from one
    point would assert an extent the data does not support.
    """
    if len(points) == 1:
        x, y = points[0]
        ewkt = f"SRID={NY_STATE_PLANE};POINT({x} {y})"
    else:
        coords = ", ".join(f"{x} {y}" for x, y in points)
        ewkt = f"SRID={NY_STATE_PLANE};LINESTRING({coords})"
    return func.ST_Transform(func.ST_GeomFromEWKT(ewkt), 4326).cast(Geography)


def load_signs(
    session: Session,
    rows: list[dict[str, Any]],
    *,
    replace_existing: bool = True,
) -> IngestReport:
    """Turn sign rows into segment sides and cleaning rules."""
    report = IngestReport(signs_seen=len(rows))
    groups: dict[tuple[str, str, str, str], list[dict[str, Any]]] = collections.defaultdict(list)

    for row in rows:
        key = _block_key(row)
        if key is None:
            report.signs_unparsed += 1
            report.unparsed_reasons["no usable block or side"] += 1
            continue
        groups[key].append(row)

    report.block_sides = len(groups)

    for (on, frm, to, side_code), signs in groups.items():
        rules: dict[tuple[tuple[int, ...], Any, Any], ParsedRule] = {}
        points: list[tuple[float, float]] = []
        first_distance_ft = 0.0

        for row in sorted(signs, key=lambda r: float(r.get("distance_from_intersection") or 0)):
            rule, why = try_parse(row.get("sign_description") or "")
            if rule is None:
                report.signs_unparsed += 1
                report.unparsed_reasons[why or "unknown"] += 1
                continue
            rules[(rule.days_of_week, rule.starts_at, rule.ends_at)] = rule
            try:
                points.append((float(row["sign_x_coord"]), float(row["sign_y_coord"])))
            except (KeyError, TypeError, ValueError):
                continue
            if len(points) == 1:
                first_distance_ft = float(row.get("distance_from_intersection") or 0)

        if not rules or not points:
            continue
        if len(points) == 1:
            report.point_only_sides += 1
        if len(rules) > 1:
            report.multi_rule_sides += 1

        segment = session.scalar(
            select(StreetSegmentSide).where(
                StreetSegmentSide.street_name == on,
                StreetSegmentSide.from_cross_street == frm,
                StreetSegmentSide.to_cross_street == to,
                StreetSegmentSide.side == _SIDE_CODES[side_code],
                StreetSegmentSide.borough == "Brooklyn",
            )
        )
        if segment is None:
            segment = StreetSegmentSide(
                street_name=on,
                from_cross_street=frm,
                to_cross_street=to,
                side=_SIDE_CODES[side_code],
                borough="Brooklyn",
            )
            session.add(segment)
            report.block_sides_created += 1
        segment.geom = _geom_expression(_extend(points, first_distance_ft))
        session.flush()

        if replace_existing:
            # Only rules this loader owns are cleared. A rule captured from a
            # photograph of the sign outranks the dataset and must survive a
            # reload, since the operator was standing in front of it.
            for existing in session.scalars(
                select(AspRule).where(
                    AspRule.segment_side_id == segment.id,
                    AspRule.source == RuleSource.nyc_signs,
                )
            ):
                session.delete(existing)
            session.flush()

        for rule in rules.values():
            session.add(
                AspRule(
                    segment_side_id=segment.id,
                    days_of_week=list(rule.days_of_week),
                    starts_at=rule.starts_at,
                    ends_at=rule.ends_at,
                    source=RuleSource.nyc_signs,
                    confidence=rule.confidence,
                    raw_sign_text=rule.raw[:2000],
                )
            )
            report.rules_created += 1

    session.commit()
    log.info("%s", report.summary())
    return report
