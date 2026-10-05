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

    @property
    def total_cents(self) -> int:
        return sum(row.amount_cents for row in self.rows)


def _money(cents: int) -> str:
    return f"${cents / 100:,.2f}"


def _local(when: datetime) -> str:
    return f"{when.astimezone(fleet_timezone()):%a %-d %b %Y, %-I:%M %p}"


def evidence_svg(sheet: EvidenceSheet) -> str:
    """The sheet, as SVG.

    Every interpolated value goes through :func:`escape`. That is not
    boilerplate here: plaza names in a real E-ZPass statement contain
    ampersands — "MTAB&T RKB" is the Robert Kennedy bridge — and an unescaped
    one produces XML that a browser refuses to render, so the evidence silently
    becomes a broken image.
    """
    height = _HEADER_HEIGHT + _ROW_HEIGHT * (len(sheet.rows) + 1) + _FOOTER_HEIGHT
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

    # Provenance, on the face of the sheet. This is the fleet's own record of an
    # E-ZPass statement, not a copy of one, and saying so is what keeps it
    # honest evidence rather than a lookalike.
    source = "from the host's E-ZPass account activity"
    if sheet.imported_at:
        source += f", imported {sheet.imported_at.astimezone(fleet_timezone()):%-d %b %Y}"
    parts.append(
        f'<text x="32" y="{total_y + 30}" font-size="11" fill="#6b7280">'
        f"Prepared by Turonomics {escape(source)}. The statement itself is "
        f"available on request.</text>"
    )
    parts.append("</svg>")
    return "".join(parts)
