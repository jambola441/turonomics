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


def parse_pairs(raw: str) -> list[tuple[str, str]]:
    """``"a=1,b=2"`` -> ``[("a", "1"), ("b", "2")]``, junk dropped.

    Shared so that every ``KEY=VALUE,KEY=VALUE`` variable in this service
    tolerates the same spacing and ignores the same malformed entries. An entry
    missing either half is dropped rather than half-applied: a map keyed by the
    empty string matches a vehicle with a blank nickname.
    """
    out: list[tuple[str, str]] = []
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair or "=" not in pair:
            continue
        left, _, right = pair.partition("=")
        left, right = left.strip(), right.strip()
        if left and right:
            out.append((left, right))
    return out


def outside_fleet() -> dict[str, str]:
    """Tags and plates on this EZPass account that are not this fleet's cars.

    ``EZPASS_OUTSIDE="00414500432=Mom's car,94979NF=Old van"``.

    A statement covers an account, not a fleet. Family cars and vehicles that
    have since left sit on the same bill, and their crossings are real money
    out — but they are not a guest's to repay and not a gap to be fixed. Before
    this, they were counted as unattributed and the tolls page asked, every
    month, for a car to bind them to. A figure the operator is told to chase
    and cannot is worse than one that is simply labelled.

    Keyed by the identifier exactly as a statement prints it, upper-cased, so a
    tag and a plate can both be listed. Not stored on the crossing: whose car
    it is can be corrected without re-importing anything.
    """
    return {
        key.upper(): label
        for key, label in parse_pairs(os.environ.get("EZPASS_OUTSIDE", ""))
    }


def toll_filing_window_days() -> int:
    """How long after a rental ends a toll can still be billed through Turo.

    Ninety days. A figure that belongs to Turo rather than to this fleet, so it
    is configurable — if they change it, nobody should have to find this
    constant in a diff.

    It is the only deadline in this system that loses money by passing
    quietly: an unbilled toll inside the window is a reminder, and the same
    toll outside it is gone.
    """
    raw = os.environ.get("TOLL_FILING_WINDOW_DAYS", "").strip()
    if not raw:
        return 90
    try:
        return max(int(raw), 0)
    except ValueError:
        return 90


def toll_overrun_minutes() -> int:
    """How long after a rental ends a crossing is still that guest's.

    A guest who brings the car back late without extending the booking leaves
    Turo's end time saying one thing and the car saying another, and every
    crossing in between falls outside the window that decides who pays. Those
    tolls are the guest's — the operator was not driving — and before this they
    were reported as money nobody owed.

    Two hours by default: long enough for a late return and the bridge on the
    way back, short enough that it cannot swallow an evening of the operator's
    own errands. Set ``TOLL_OVERRUN_GRACE_MINUTES=0`` to switch it off, which
    is worth knowing about — it attributes money to a guest on an inference,
    and the inference is visible on the page precisely so it can be overruled.

    It never reaches past the next rental of that car: once somebody else has
    the keys, the crossing is theirs.
    """
    raw = os.environ.get("TOLL_OVERRUN_GRACE_MINUTES", "").strip()
    if not raw:
        return 120
    try:
        return max(int(raw), 0)
    except ValueError:
        return 120


def asp_alert_lead_minutes() -> list[int]:
    """How far ahead of a deadline to warn, longest first.

    Two by default, each saying something different: twelve hours out is "move
    it tonight, at a civilised hour", and one hour out is "go now". The earlier
    default here was 720/120/60/15, which is four notifications per car per
    cleaning night — sixteen across this fleet, which teaches you to swipe them
    away. Fifteen minutes is also past useful: finding another legal spot in
    this neighbourhood takes longer than that.

    An alert for a deadline already passed is separate and always sent; see
    ``notify.alerts``.
    """
    raw = os.environ.get("ASP_ALERT_LEAD_MINUTES", "720,60")
    return sorted({int(p) for p in raw.split(",") if p.strip()}, reverse=True)


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


def invert_map() -> bool:
    """Whether to darken the basemap in CSS.

    Off unless asked for. An earlier version derived this from the theme name —
    inverting anything not on a list of known-dark styles — which meant picking
    a light style silently got you a dark map, and the only way to see the
    style you chose was to edit the code. A display preference should be a
    preference, not an inference.
    """
    return os.environ.get("MAP_INVERT", "").strip().lower() in {"1", "true", "yes"}


def map_tiles() -> dict[str, object]:
    """Which basemap the fleet view should draw, and whether to invert it."""
    key = os.environ.get("THUNDERFOREST_API_KEY", "").strip()
    if not key:
        return {
            "url": OSM_TILES,
            "attribution": OSM_ATTRIBUTION,
            "invert": invert_map(),
            "max_zoom": 19,
        }

    style = os.environ.get("THUNDERFOREST_STYLE", DEFAULT_THUNDERFOREST_STYLE).strip()
    return {
        "url": THUNDERFOREST_TILES.format(style=style, key=key),
        "attribution": THUNDERFOREST_ATTRIBUTION,
        "invert": invert_map(),
        "max_zoom": 22,
    }


# ---------------------------------------------------------------------------
# Alerts
# ---------------------------------------------------------------------------

DEFAULT_SITE_URL = "https://turonomics-site.onrender.com/fleet/"


def site_url() -> str:
    """Where a notification should send you when you tap it.

    The run sheet is a static site on a different host from this API, so the
    API cannot derive this from its own request. Note this is the *web* URL,
    not the API URL — confusing the two is how the Gmail callback went wrong.
    """
    return os.environ.get("SITE_URL", "").strip() or DEFAULT_SITE_URL


def vapid_private_key() -> str | None:
    """The application server key, or ``None`` when push is not configured.

    Absent is a supported state, not an error: the alert rules still run and
    still log what they would have sent, which is how the timing gets checked
    against a real fleet before any key exists. ``python -m turonomics_api.cli
    vapid-keys`` prints a pair to set here.
    """
    return os.environ.get("VAPID_PRIVATE_KEY", "").strip() or None


def vapid_subject() -> str:
    """Contact of record for the push service, per RFC 8292.

    A push service uses it to reach the operator of a misbehaving application
    server. It must be a ``mailto:`` or ``https:`` URI; a bare address is
    rejected, and some services reject it with no explanation.
    """
    raw = os.environ.get("VAPID_SUBJECT", "").strip()
    if not raw:
        return "mailto:alerts@turonomics.invalid"
    if raw.startswith(("mailto:", "https://")):
        return raw
    return f"mailto:{raw}"
