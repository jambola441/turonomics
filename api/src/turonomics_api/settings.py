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
