"""When must this car move?

Street-cleaning rules are local wall-clock: a sign reading "Tue & Fri
11:30am-1pm" means 11:30am in July and 11:30am in January. So the search walks
forward in *local* days and constructs each candidate in the fleet timezone,
rather than doing arithmetic on UTC instants and converting at the end. The two
agree for most of the year and disagree by an hour twice, which is exactly the
kind of bug that surfaces as a ticket in November.

The deadline is the *start* of the window: you must be gone before the sweeper
arrives, not before it leaves.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from turonomics_api.settings import fleet_timezone

# A rule repeating weekly always recurs within 7 days; the extra days cover a
# run of consecutive suspensions (a snow week).
MAX_LOOKAHEAD_DAYS = 28


@dataclass(frozen=True)
class Rule:
    """A cleaning window, as read off the sign."""

    days_of_week: tuple[int, ...]  # ISO: Monday=1 … Sunday=7
    starts_at: time
    ends_at: time


@dataclass(frozen=True)
class CleaningWindow:
    starts_at: datetime  # tz-aware; the move-by deadline
    ends_at: datetime
    rule: Rule

    @property
    def crosses_midnight(self) -> bool:
        return self.ends_at.date() != self.starts_at.date()


def _localise(day: date, moment: time, tz: ZoneInfo) -> datetime:
    return datetime.combine(day, moment, tzinfo=tz)


def next_cleaning_window(
    rules: list[Rule],
    *,
    now: datetime,
    suspended_dates: frozenset[date] | set[date] | None = None,
    tz: ZoneInfo | None = None,
) -> CleaningWindow | None:
    """The next window that has not already started, or ``None``.

    A window already in progress is not returned: the deadline for it has
    passed, and reporting it as upcoming would tell the operator they have time
    when they do not. The caller decides what to say about a car sitting in a
    live cleaning window.
    """
    if not rules:
        return None

    tz = tz or fleet_timezone()
    suspended = frozenset(suspended_dates or ())
    local_now = now.astimezone(tz)

    best: CleaningWindow | None = None
    for offset in range(MAX_LOOKAHEAD_DAYS):
        day = (local_now + timedelta(days=offset)).date()
        if day in suspended:
            continue
        iso_day = day.isoweekday()

        for rule in rules:
            if iso_day not in rule.days_of_week:
                continue

            starts = _localise(day, rule.starts_at, tz)
            if starts <= local_now:
                continue

            ends = _localise(day, rule.ends_at, tz)
            if ends <= starts:
                # A window written as 11pm-1am ends on the following day.
                ends = _localise(day + timedelta(days=1), rule.ends_at, tz)

            if best is None or starts < best.starts_at:
                best = CleaningWindow(starts_at=starts, ends_at=ends, rule=rule)

        if best is not None:
            # Any window on a later day starts later, so the earliest found on
            # the first qualifying day wins.
            return best

    return None


def is_in_cleaning_window(
    rules: list[Rule],
    *,
    now: datetime,
    suspended_dates: frozenset[date] | set[date] | None = None,
    tz: ZoneInfo | None = None,
) -> bool:
    """Whether cleaning is happening right now — i.e. the car is already late."""
    tz = tz or fleet_timezone()
    suspended = frozenset(suspended_dates or ())
    local_now = now.astimezone(tz)

    # Check today and yesterday, so a window that began before midnight counts.
    for offset in (0, -1):
        day = (local_now + timedelta(days=offset)).date()
        if day in suspended:
            continue
        for rule in rules:
            if day.isoweekday() not in rule.days_of_week:
                continue
            starts = _localise(day, rule.starts_at, tz)
            ends = _localise(day, rule.ends_at, tz)
            if ends <= starts:
                ends = _localise(day + timedelta(days=1), rule.ends_at, tz)
            if starts <= local_now < ends:
                return True
    return False
