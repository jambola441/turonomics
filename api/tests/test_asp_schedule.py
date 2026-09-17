"""Street-cleaning deadline computation.

The DST cases are the point of this file. A sign says 11:30am all year; a naive
UTC implementation moves that by an hour for half of it, and the failure shows
up as a $65 ticket rather than as an exception.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

import pytest

from turonomics_api.asp.schedule import Rule, is_in_cleaning_window, next_cleaning_window

NYC = ZoneInfo("America/New_York")

MON, TUE, WED, THU, FRI = 1, 2, 3, 4, 5

# 590 Bergen St, north side — the real shape of a Prospect Heights sign.
BERGEN_N = Rule(days_of_week=(TUE, FRI), starts_at=time(8, 0), ends_at=time(9, 30))
# Dean St, south side — the other half of the pair that makes side-of-street
# resolution matter.
DEAN_S = Rule(days_of_week=(MON, THU), starts_at=time(11, 30), ends_at=time(13, 0))


def local(y: int, m: int, d: int, hh: int, mm: int = 0) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=NYC)


def test_finds_todays_window_when_it_has_not_started():
    # Tuesday 07:42, cleaning at 08:00 — 18 minutes away.
    got = next_cleaning_window([BERGEN_N], now=local(2026, 9, 15, 7, 42))
    assert got is not None
    assert got.starts_at == local(2026, 9, 15, 8, 0)


def test_a_window_already_started_is_not_reported_as_upcoming():
    """Reporting a live window as 'upcoming' would tell the operator they have
    time when the sweeper is already on the block."""
    got = next_cleaning_window([BERGEN_N], now=local(2026, 9, 15, 8, 30))
    assert got is not None
    assert got.starts_at == local(2026, 9, 18, 8, 0)  # the following Friday
    assert is_in_cleaning_window([BERGEN_N], now=local(2026, 9, 15, 8, 30)) is True


def test_rolls_to_the_next_listed_day():
    # Tuesday, just after the window closed.
    got = next_cleaning_window([BERGEN_N], now=local(2026, 9, 15, 9, 31))
    assert got.starts_at == local(2026, 9, 18, 8, 0)


def test_the_earliest_of_several_rules_wins():
    got = next_cleaning_window([BERGEN_N, DEAN_S], now=local(2026, 9, 14, 6, 0))  # Monday
    assert got.starts_at == local(2026, 9, 14, 11, 30)  # Monday's Dean St rule


def test_a_suspension_skips_that_day_entirely():
    got = next_cleaning_window(
        [BERGEN_N],
        now=local(2026, 9, 15, 7, 0),
        suspended_dates={date(2026, 9, 15)},
    )
    assert got.starts_at == local(2026, 9, 18, 8, 0)


def test_consecutive_suspensions_are_survived():
    """A snow week suspends several days in a row; the search must not give up
    after one."""
    got = next_cleaning_window(
        [BERGEN_N],
        now=local(2026, 9, 15, 7, 0),
        suspended_dates={date(2026, 9, 15), date(2026, 9, 18), date(2026, 9, 22)},
    )
    assert got.starts_at == local(2026, 9, 25, 8, 0)


def test_no_rules_means_no_deadline():
    assert next_cleaning_window([], now=local(2026, 9, 15, 7, 0)) is None


# ---------------------------------------------------------------------------
# DST — the reason this module does local-day arithmetic
# ---------------------------------------------------------------------------


def test_deadline_stays_at_the_posted_local_time_across_the_autumn_change():
    """Clocks go back 01:00 Sun 1 Nov 2026. The sign still says 11:30am."""
    got = next_cleaning_window([DEAN_S], now=local(2026, 10, 30, 9, 0))  # Fri before
    assert got.starts_at == local(2026, 11, 2, 11, 30)  # Monday after
    assert got.starts_at.hour == 11 and got.starts_at.minute == 30
    # And in UTC it has genuinely shifted by an hour, which is the whole point.
    assert got.starts_at.astimezone(UTC).hour == 16  # EST, not EDT


def test_deadline_stays_at_the_posted_local_time_across_the_spring_change():
    """Clocks go forward 08 Mar 2026."""
    before = next_cleaning_window([DEAN_S], now=local(2026, 3, 5, 12, 0))  # Thu after window
    assert before.starts_at == local(2026, 3, 9, 11, 30)  # Monday after the change
    assert before.starts_at.astimezone(UTC).hour == 15  # EDT


def test_utc_arithmetic_would_have_got_this_wrong():
    """Pins the bug this design avoids: the same wall-clock deadline is a
    different number of UTC hours either side of the change."""
    winter = next_cleaning_window([DEAN_S], now=local(2026, 11, 30, 9, 0))
    summer = next_cleaning_window([DEAN_S], now=local(2026, 6, 29, 9, 0))
    assert winter.starts_at.hour == summer.starts_at.hour == 11
    assert winter.starts_at.astimezone(UTC).hour != summer.starts_at.astimezone(UTC).hour


# ---------------------------------------------------------------------------
# Odd but real sign shapes
# ---------------------------------------------------------------------------


def test_a_window_crossing_midnight_ends_on_the_following_day():
    overnight = Rule(days_of_week=(WED,), starts_at=time(23, 0), ends_at=time(1, 0))
    got = next_cleaning_window([overnight], now=local(2026, 9, 16, 20, 0))
    assert got.starts_at == local(2026, 9, 16, 23, 0)
    assert got.ends_at == local(2026, 9, 17, 1, 0)
    assert got.crosses_midnight is True


def test_being_inside_an_overnight_window_is_detected_after_midnight():
    overnight = Rule(days_of_week=(WED,), starts_at=time(23, 0), ends_at=time(1, 0))
    assert is_in_cleaning_window([overnight], now=local(2026, 9, 17, 0, 30)) is True
    assert is_in_cleaning_window([overnight], now=local(2026, 9, 17, 1, 30)) is False


def test_a_utc_input_is_interpreted_in_the_fleet_zone():
    """Callers pass whatever they have; only the fleet zone decides the day."""
    # 03:00 UTC on Wed 16 Sep is 23:00 local on Tue 15 Sep.
    got = next_cleaning_window([BERGEN_N], now=datetime(2026, 9, 16, 3, 0, tzinfo=UTC))
    assert got.starts_at == local(2026, 9, 18, 8, 0)


@pytest.mark.parametrize("hour", [0, 6, 12, 18, 23])
def test_a_deadline_is_always_in_the_future(hour: int) -> None:
    now = local(2026, 9, 15, hour)
    got = next_cleaning_window([BERGEN_N, DEAN_S], now=now)
    assert got is not None and got.starts_at > now
