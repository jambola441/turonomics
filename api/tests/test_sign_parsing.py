"""NYC parking-sign text parsing.

Every string in this file is real: taken verbatim from the Open Data sign
dataset for zip 11238, including the city's own misspellings. The corpus there
is 3,012 street-cleaning signs across 98 distinct strings, and the parser
handles all of them — but coverage is not correctness, so the awkward shapes
are asserted by value here.
"""

from __future__ import annotations

from datetime import time

import pytest

from turonomics_api.asp.signs import SignParseError, parse_sign_description, try_parse

MON, TUE, WED, THU, FRI, SAT, SUN = range(1, 8)


@pytest.mark.parametrize(
    ("description", "days", "start", "end"),
    [
        # The common case, ~80% of the corpus.
        (
            "NO PARKING (SANITATION BROOM SYMBOL) MONDAY THURSDAY 11:30AM-1PM <-> (SUPERSEDES SP-850C)",
            (MON, THU),
            time(11, 30),
            time(13, 0),
        ),
        # The space before the arrow is missing on hundreds of signs.
        (
            "NO PARKING (SANITATION BROOM SYMBOL) TUESDAY 9:30AM-11AM<->",
            (TUE,),
            time(9, 30),
            time(11, 0),
        ),
        (
            "NO PARKING (SANITATION BROOM SYMBOL) TUESDAY 8:30AM-10AM-->",
            (TUE,),
            time(8, 30),
            time(10, 0),
        ),
        # Names no days at all; the exclusion carries the meaning.
        (
            "NO PARKING (SANITATION BROOM SYMBOL) 8:30AM-9AM EXCEPT SUNDAY <->",
            (MON, TUE, WED, THU, FRI, SAT),
            time(8, 30),
            time(9, 0),
        ),
        # Overnight, with the time written as a word.
        (
            "NO PARKING (SANITATION BROOM SYMBOL) MOON & STARS (SYMBOLS) TUESDAY THURSDAY SATURDAY MIDNIGHT-3AM <->",
            (TUE, THU, SAT),
            time(0, 0),
            time(3, 0),
        ),
        (
            "NO PARKING (SANITATION BROOM SYMBOL) W/ (MOON/STARS SYMBOLS) MONDAY WEDNESDAY FRIDAY 3AM-6AM <->",
            (MON, WED, FRI),
            time(3, 0),
            time(6, 0),
        ),
        # "THURDAY" is on real signs. Refusing it would silently drop the rule
        # for those blocks, which is the failure mode this project is about.
        (
            "NO PARKING (SANITATION BROOM SYMBOL) MONDAY WEDNESDAY THURDAY 9:30AM-11AM -->",
            (MON, WED, THU),
            time(9, 30),
            time(11, 0),
        ),
        # An older sign format: abbreviated days, "TO" instead of a dash.
        (
            "NO PARKING (SANITATION BROOM SYMBOL) 11:30AM TO 1PM MON & THURS <----> (SUPERSEDED BY SP-850C)",
            (MON, THU),
            time(11, 30),
            time(13, 0),
        ),
        # A typo inside the time itself.
        (
            "NO PARKING (SANITATION BROOM SYMBOL) TUESDAY FRIDAY 10A M-11:30AM -->",
            (TUE, FRI),
            time(10, 0),
            time(11, 30),
        ),
        (
            "NO PARKING (SANITATION BROOM SYMBOL) MONDAY TUESDAY THURSDAY FRIDAY 8AM-8:30AM -->",
            (MON, TUE, THU, FRI),
            time(8, 0),
            time(8, 30),
        ),
        # Afternoon: 1PM must not become 01:00.
        (
            "NO PARKING (SANITATION BROOM SYMBOL) MONDAY THURSDAY 1PM-2:30PM <->",
            (MON, THU),
            time(13, 0),
            time(14, 30),
        ),
    ],
)
def test_real_signs_parse_to_the_right_rule(
    description: str, days: tuple[int, ...], start: time, end: time
) -> None:
    rule = parse_sign_description(description)
    assert rule.days_of_week == days
    assert rule.starts_at == start
    assert rule.ends_at == end


def test_the_hyphen_in_a_time_range_is_not_mistaken_for_an_arrow() -> None:
    """Regression: an arrow pattern of "a run of hyphens" also matches the
    separator in 8:30AM-10AM, which silently destroyed every time range in the
    corpus while still parsing the one format that spells it "TO"."""
    rule = parse_sign_description(
        "NO PARKING (SANITATION BROOM SYMBOL) TUESDAY FRIDAY 8:30AM-10AM <->"
    )
    assert (rule.starts_at, rule.ends_at) == (time(8, 30), time(10, 0))


@pytest.mark.parametrize(
    ("description", "reason"),
    [
        ("NO STANDING ANYTIME -->", "not a street-cleaning sign"),
        ("2 HOUR PARKING 9AM-7PM EXCEPT SUNDAY", "not a street-cleaning sign"),
        ("NO PARKING (SANITATION BROOM SYMBOL) MONDAY THURSDAY <->", "no time range"),
        ("NO PARKING (SANITATION BROOM SYMBOL) 11:30AM-1PM <->", "no days named"),
        ("", "empty description"),
    ],
)
def test_it_refuses_rather_than_guesses(description: str, reason: str) -> None:
    """A wrong rule produces a confidently wrong deadline, which is worse than
    an admitted gap. Anything unclear must surface as a gap."""
    rule, why = try_parse(description)
    assert rule is None
    assert reason in why
    with pytest.raises(SignParseError):
        parse_sign_description(description)


def test_midnight_and_noon_are_not_confused() -> None:
    midnight = parse_sign_description(
        "NO PARKING (SANITATION BROOM SYMBOL) MONDAY MIDNIGHT-3AM <->"
    )
    assert midnight.starts_at == time(0, 0)
    assert midnight.crosses_midnight is False

    twelve_pm = parse_sign_description("NO PARKING (SANITATION BROOM SYMBOL) MONDAY 12PM-1PM <->")
    assert twelve_pm.starts_at == time(12, 0)

    twelve_am = parse_sign_description("NO PARKING (SANITATION BROOM SYMBOL) MONDAY 12AM-1AM <->")
    assert twelve_am.starts_at == time(0, 0)


def test_a_window_running_past_midnight_is_flagged() -> None:
    rule = parse_sign_description("NO PARKING (SANITATION BROOM SYMBOL) MONDAY 11PM-1AM <->")
    assert rule.crosses_midnight is True


def test_the_overnight_pictogram_is_recorded() -> None:
    rule = parse_sign_description(
        "NO PARKING (SANITATION BROOM SYMBOL) MOON & STARS (SYMBOLS) MONDAY MIDNIGHT-3AM <->"
    )
    assert rule.overnight_symbol is True
    assert rule.confidence == 1.0


def test_a_pictogram_disagreeing_with_the_hours_lowers_confidence() -> None:
    """The moon-and-stars symbol says overnight; 11:30am does not. Trust the
    hours, but do not claim full confidence."""
    rule = parse_sign_description(
        "NO PARKING (SANITATION BROOM SYMBOL) MOON & STARS (SYMBOLS) MONDAY 11:30AM-1PM <->"
    )
    assert rule.starts_at == time(11, 30)
    assert rule.confidence < 1.0


def test_superseded_bookkeeping_does_not_leak_into_the_rule() -> None:
    """(SUPERSEDES SP-850C) is about sign designs, and contains digits that
    could be mistaken for times."""
    a = parse_sign_description(
        "NO PARKING (SANITATION BROOM SYMBOL) MONDAY 9AM-10:30AM <-> (SUPERSEDES SP-379C & SP-454C)"
    )
    b = parse_sign_description("NO PARKING (SANITATION BROOM SYMBOL) MONDAY 9AM-10:30AM <->")
    assert (a.days_of_week, a.starts_at, a.ends_at) == (b.days_of_week, b.starts_at, b.ends_at)
