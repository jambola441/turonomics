"""An evidence sheet for the crossings an invoice is asking a guest to pay.

Turo attaches evidence images to each line item of a reimbursement invoice —
the real payload for a filed toll invoice carries
``evidenceImagesResponse.images[]`` on its ``TOLL_REIMBURSEMENT`` line. A toll
invoice filed without one is a number with nothing behind it, and a guest
disputing it has nothing to look at.

This renders what the fleet knows: which crossings, at which plaza, at what
local time, for how much. It is **not** a facsimile of an E-ZPass statement
page, and deliberately so. A generated image dressed up as somebody else's
document misrepresents where the figures came from, and would be worse than no
evidence the first time a guest looked closely. The sheet says on its face that
it is this app's own ledger, names the statement import it came from, and leaves
the reader free to ask for the statement itself.

SVG rather than a raster, because a dependency on an imaging library to draw
nine rows of text is not worth carrying, and the extension — which is what
talks to Turo — is running in a browser that rasterises SVG natively.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from xml.sax.saxutils import escape

from turonomics_api.settings import fleet_timezone

# Deliberately plain. The sheet is read next to Turo's own invoice UI, so it
# should look like a document rather than like a brand.
_WIDTH = 760
_ROW_HEIGHT = 28
_HEADER_HEIGHT = 132
_FOOTER_HEIGHT = 56


@dataclass(frozen=True)
class EvidenceRow:
    occurred_at: datetime
    plaza: str
    amount_cents: int


# How much of the track an inset shows, in metres from the plaza. Small on
# purpose: the sheet goes to a guest and to Turo, and a guest's whole route is
# nobody's business but theirs. The track outside this circle is not drawn, and
# is not in the data this module is given.
INSET_RADIUS_M = 1200.0
_INSET_W = 220
_INSET_MAP_H = 130
_INSET_CAPTION_H = 66
_INSET_GAP = 18
_INSET_COLUMNS = 3
_INSET_ROW = _INSET_MAP_H + _INSET_CAPTION_H + 14
_INSETS_HEADER = 62


@dataclass(frozen=True)
class Inset:
    """The tracker's positions within a short distance of one plaza."""

    # What the statement calls it, and the researched name if there is one.
    plaza: str
    name: str | None
    lat: float
    lon: float
    # Polylines of (lat, lon), already clipped to the circle.
    segments: Sequence[Sequence[tuple[float, float]]]
    # The nearest the tracker came to the plaza at any point in the rental.
    closest_m: int
    # When the crossings at this plaza were charged, for the caption.
    times: Sequence[datetime]
    # The plaza's position is approximate (medium confidence).
    approximate: bool = False


@dataclass(frozen=True)
class EvidenceSheet:
    reservation_id: str | None
    guest_name: str | None
    vehicle: str
    plate: str | None
    starts_at: datetime
    ends_at: datetime
    rows: Sequence[EvidenceRow]
    # When the statement these came from was imported, so the sheet can say
    # how it knows rather than simply asserting.
    imported_at: datetime | None = None
    # Where the car's tracker was near each plaza, when it was available. Empty
    # means no location data, and the sheet is the plain table it always was.
    insets: Sequence[Inset] = ()
    # "bouncie" (a full GPS track) or "telemetry" (stored positions, a fix every
    # few minutes), so the sheet says what the lines are.
    track_source: str | None = None
    # Plazas that had a track but did not fit the sheet.
    insets_omitted: int = 0

    @property
    def total_cents(self) -> int:
        return sum(row.amount_cents for row in self.rows)


def _money(cents: int) -> str:
    return f"${cents / 100:,.2f}"


def _local(when: datetime) -> str:
    return f"{when.astimezone(fleet_timezone()):%a %-d %b %Y, %-I:%M %p}"


def _project(
    lat: float, lon: float, centre: tuple[float, float], scale: float, cx: float, cy: float
) -> tuple[float, float]:
    """Metres east/north of the plaza, as pixels. Flat, which is exact enough at
    a kilometre."""
    east = (lon - centre[1]) * 111_320 * math.cos(math.radians(centre[0]))
    north = (lat - centre[0]) * 111_320
    return cx + east * scale, cy - north * scale


def _fit(text: str, limit: int) -> str:
    """Cut to what fits a caption column, with an ellipsis. A caption that runs
    into the next inset reads as one sentence about two plazas."""
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    # At a word, so it does not end on an open bracket or half a name.
    if " " in cut and cut.rfind(" ") > limit // 2:
        cut = cut[: cut.rfind(" ")]
    return cut.rstrip(" ([,\u00b7-") + "\u2026"


def _inset_svg(inset: Inset, x: float, y: float) -> str:
    cx, cy = x + _INSET_W / 2, y + _INSET_MAP_H / 2
    radius_px = min(_INSET_W, _INSET_MAP_H) / 2 - 6
    scale = radius_px / INSET_RADIUS_M
    centre = (inset.lat, inset.lon)
    out = [
        f'<rect x="{x}" y="{y}" width="{_INSET_W}" height="{_INSET_MAP_H}" '
        f'fill="#f9fafb" stroke="#d1d5db"/>',
        f'<circle cx="{cx}" cy="{cy}" r="{radius_px:.1f}" fill="#ffffff" '
        f'stroke="#9ca3af" stroke-dasharray="3 3"/>',
    ]
    for segment in inset.segments:
        points = " ".join(
            f"{px:.1f},{py:.1f}"
            for px, py in (_project(lat, lon, centre, scale, cx, cy) for lat, lon in segment)
        )
        out.append(
            f'<polyline points="{points}" fill="none" stroke="#2563eb" stroke-width="2.5" '
            f'stroke-linejoin="round" stroke-linecap="round"/>'
        )
    out.append(
        f'<circle cx="{cx}" cy="{cy}" r="5" fill="#dc2626" stroke="#ffffff" stroke-width="1.5"/>'
    )
    bar = 500 * scale
    base = y + _INSET_MAP_H - 10
    out.append(
        f'<line x1="{x + 8}" y1="{base}" x2="{x + 8 + bar:.1f}" y2="{base}" '
        f'stroke="#374151" stroke-width="2"/>'
        f'<text x="{x + 8 + bar + 4:.1f}" y="{base + 4}" font-size="9" fill="#374151">500 m</text>'
    )
    label = inset.plaza if not inset.name else f"{inset.plaza} \u00b7 {inset.name}"
    near = f"tracker came within {inset.closest_m} m"
    # The first time and a count, not a list: every crossing is in the table
    # above with its own time, and a list longer than the column is cut, which
    # once dropped the count and made the caption misleading.
    stamps = f"{inset.times[0].astimezone(fleet_timezone()):%-d %b %-I:%M %p}"
    if len(inset.times) > 1:
        stamps += f" +{len(inset.times) - 1} more"
    ty = y + _INSET_MAP_H + 15
    out.append(
        f'<text x="{x}" y="{ty}" font-size="11" font-weight="600" fill="#111827">'
        f"{escape(_fit(label, 34))}</text>"
        f'<text x="{x}" y="{ty + 14}" font-size="10" fill="#374151">{escape(near)}</text>'
    )
    line = ty + 27
    if inset.approximate:
        out.append(
            f'<text x="{x}" y="{line}" font-size="10" fill="#374151">'
            f"plaza position approximate</text>"
        )
        line += 13
    out.append(
        f'<text x="{x}" y="{line}" font-size="10" fill="#6b7280">'
        f"{escape(_fit('charged ' + stamps, 40))}</text>"
    )
    return "".join(out)


def _insets_height(sheet: EvidenceSheet) -> int:
    if not sheet.insets:
        return 0
    rows = math.ceil(len(sheet.insets) / _INSET_COLUMNS)
    extra = 16 if sheet.insets_omitted else 0
    return _INSETS_HEADER + rows * _INSET_ROW + extra


def evidence_svg(sheet: EvidenceSheet) -> str:
    """The sheet, as SVG.

    Every interpolated value goes through :func:`escape`. That is not
    boilerplate here: plaza names in a real E-ZPass statement contain
    ampersands — "MTAB&T RKB" is the Robert Kennedy bridge — and an unescaped
    one produces XML that a browser refuses to render, so the evidence silently
    becomes a broken image.
    """
    height = (
        _HEADER_HEIGHT
        + _ROW_HEIGHT * (len(sheet.rows) + 1)
        + _FOOTER_HEIGHT
        + _insets_height(sheet)
    )
    who = sheet.guest_name or "the guest"
    plate = f" · {sheet.plate}" if sheet.plate else ""
    # Both ends through the same formatter. Written differently at first, so
    # the sheet read "9 Jul 2026, 7:00 AM — Sun 12 Jul 2026, 2:00 PM", with a
    # weekday on one end and not the other.
    window = f"{_local(sheet.starts_at)} — {_local(sheet.ends_at)}"
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{_WIDTH}" height="{height}" '
        f'viewBox="0 0 {_WIDTH} {height}" font-family="Helvetica, Arial, sans-serif">',
        f'<rect width="{_WIDTH}" height="{height}" fill="#ffffff"/>',
        '<text x="32" y="44" font-size="20" font-weight="600" fill="#111827">'
        "Toll crossings during this trip</text>",
        f'<text x="32" y="70" font-size="13" fill="#374151">'
        f"{escape(sheet.vehicle)}{escape(plate)} · {escape(who)}</text>",
        f'<text x="32" y="90" font-size="13" fill="#374151">Trip: {escape(window)}</text>',
    ]
    if sheet.reservation_id:
        parts.append(
            f'<text x="32" y="110" font-size="13" fill="#374151">'
            f"Reservation {escape(sheet.reservation_id)}</text>"
        )

    y = _HEADER_HEIGHT
    parts.append(
        f'<line x1="32" y1="{y - 12}" x2="{_WIDTH - 32}" y2="{y - 12}" stroke="#d1d5db"/>'
    )
    parts.append(
        f'<text x="32" y="{y + 6}" font-size="12" font-weight="600" fill="#6b7280">'
        f"When</text>"
        f'<text x="300" y="{y + 6}" font-size="12" font-weight="600" fill="#6b7280">'
        f"Plaza</text>"
        f'<text x="{_WIDTH - 32}" y="{y + 6}" font-size="12" font-weight="600" '
        f'fill="#6b7280" text-anchor="end">Amount</text>'
    )

    for index, row in enumerate(sheet.rows, start=1):
        row_y = y + 6 + _ROW_HEIGHT * index
        shade = "#f9fafb" if index % 2 else "#ffffff"
        parts.append(
            f'<rect x="32" y="{row_y - 19}" width="{_WIDTH - 64}" '
            f'height="{_ROW_HEIGHT}" fill="{shade}"/>'
        )
        parts.append(
            f'<text x="32" y="{row_y}" font-size="13" fill="#111827">'
            f"{escape(_local(row.occurred_at))}</text>"
            f'<text x="300" y="{row_y}" font-size="13" fill="#111827">'
            f"{escape(row.plaza)}</text>"
            f'<text x="{_WIDTH - 32}" y="{row_y}" font-size="13" fill="#111827" '
            f'text-anchor="end">{_money(row.amount_cents)}</text>'
        )

    total_y = y + 6 + _ROW_HEIGHT * (len(sheet.rows) + 1)
    parts.append(
        f'<line x1="32" y1="{total_y - 20}" x2="{_WIDTH - 32}" y2="{total_y - 20}" '
        f'stroke="#d1d5db"/>'
    )
    parts.append(
        f'<text x="32" y="{total_y}" font-size="14" font-weight="600" fill="#111827">'
        f"{len(sheet.rows)} crossing(s)</text>"
        f'<text x="{_WIDTH - 32}" y="{total_y}" font-size="14" font-weight="600" '
        f'fill="#111827" text-anchor="end">{_money(sheet.total_cents)}</text>'
    )

    below = total_y + 30
    if sheet.insets:
        top = total_y + 36
        what = (
            "the car's GPS track"
            if sheet.track_source == "bouncie"
            else "positions the car's tracker stored (a fix every few minutes)"
        )
        parts.append(
            f'<text x="32" y="{top}" font-size="14" font-weight="600" fill="#111827">'
            f"Where the car's tracker was near each plaza</text>"
            f'<text x="32" y="{top + 17}" font-size="10.5" fill="#6b7280">'
            f"Blue is {escape(what)}, shown only within 1.2 km of the plaza. The red "
            f"marker is where the plaza is, not where the car was when it was charged.</text>"
            f'<text x="32" y="{top + 31}" font-size="10.5" fill="#6b7280">'
            f"The rest of the trip is not shown.</text>"
        )
        for index, inset in enumerate(sheet.insets):
            column, grid_row = index % _INSET_COLUMNS, index // _INSET_COLUMNS
            parts.append(
                _inset_svg(
                    inset,
                    32 + column * (_INSET_W + _INSET_GAP),
                    top + 44 + grid_row * _INSET_ROW,
                )
            )
        below = total_y + 30 + _insets_height(sheet)
        if sheet.insets_omitted:
            parts.append(
                f'<text x="32" y="{below - 8}" font-size="10.5" fill="#6b7280">'
                f"{sheet.insets_omitted} more plaza(s) not drawn.</text>"
            )

    # Provenance, on the face of the sheet. This is the fleet's own record of an
    # E-ZPass statement, not a copy of one, and saying so is what keeps it
    # honest evidence rather than a lookalike.
    source = "from the host's E-ZPass account activity"
    if sheet.imported_at:
        source += f", imported {sheet.imported_at.astimezone(fleet_timezone()):%-d %b %Y}"
    parts.append(
        f'<text x="32" y="{below}" font-size="11" fill="#6b7280">'
        f"Prepared by Turonomics {escape(source)}. The statement itself is "
        f"available on request.</text>"
    )
    parts.append("</svg>")
    return "".join(parts)
