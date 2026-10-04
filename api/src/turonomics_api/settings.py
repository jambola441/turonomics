"""Runtime configuration."""

from __future__ import annotations

import os
from functools import lru_cache
from zoneinfo import ZoneInfo


@lru_cache(maxsize=1)
def fleet_timezone() -> ZoneInfo:
    """The zone street-cleaning rules are written in.

    Explicit rather than inherited from the process clock, and deliberately not
    taken from Bouncie's ``stats.localTimeZone`` — that field is a UTC offset
    ("-0400"), which cannot express a DST transition. A sign saying 11:30am
    means 11:30am in both June and January; computing in UTC would move every
    deadline by an hour for half the year.
    """
    return ZoneInfo(os.environ.get("FLEET_TIMEZONE", "America/New_York"))


def asp_alert_lead_minutes() -> list[int]:
    raw = os.environ.get("ASP_ALERT_LEAD_MINUTES", "720,120,60,15")
    return sorted((int(p) for p in raw.split(",") if p.strip()), reverse=True)


# Thunderforest's dark themes are better suited to this than a generic basemap —
# they pick out transit routes, which is context when a car is parked near a
# bus lane. They need a key, and the key reaches the browser whatever we do, so
# it is served from here rather than committed: one place to set it, and no
# secret in the repo.
#
# Defaulting to OpenStreetMap rather than to Thunderforest-without-a-key is
# deliberate. Keyless Thunderforest does currently answer, but relying on an
# undocumented allowance is how the Carto breakage happened — that worked
# anonymously too, right up until it did not, and then failed invisibly.
OSM_TILES = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
OSM_ATTRIBUTION = "&copy; OpenStreetMap contributors"

THUNDERFOREST_TILES = (
    "https://tile.thunderforest.com/{style}/{{z}}/{{x}}/{{y}}.png?apikey={key}"
)
THUNDERFOREST_ATTRIBUTION = (
    "Maps &copy; Thunderforest, Data &copy; OpenStreetMap contributors"
)
DEFAULT_THUNDERFOREST_STYLE = "transport-dark"

# Themes that are already dark. Anything else gets inverted in CSS, which is
# what makes a light basemap usable here — and would wreck an already-dark one.
DARK_STYLES = frozenset({"transport-dark", "spinal-map"})


def map_tiles() -> dict[str, object]:
    """Which basemap the fleet view should draw, and whether to invert it."""
    key = os.environ.get("THUNDERFOREST_API_KEY", "").strip()
    if not key:
        return {"url": OSM_TILES, "attribution": OSM_ATTRIBUTION, "invert": True, "max_zoom": 19}

    style = os.environ.get("THUNDERFOREST_STYLE", DEFAULT_THUNDERFOREST_STYLE).strip()
    return {
        "url": THUNDERFOREST_TILES.format(style=style, key=key),
        "attribution": THUNDERFOREST_ATTRIBUTION,
        "invert": style not in DARK_STYLES,
        "max_zoom": 22,
    }
