"""Parse NYC parking-sign text into structured street-cleaning rules.

Input is the ``sign_description`` field of the Open Data sign dataset
(``nfid-uabd``). The regular case looks like::

    NO PARKING (SANITATION BROOM SYMBOL) MONDAY THURSDAY 11:30AM-1PM <->

but the real corpus is dirtier than that, and the dirt is the interesting part:

* the space before the arrow is sometimes missing (``9:30AM-11AM<->``)
* overnight rules carry a ``MOON & STARS (SYMBOLS)`` prefix
* some rules name no days at all and say ``EXCEPT SUNDAY`` instead
* times are occasionally words (``MIDNIGHT``) or mistyped (``10A M-11:30AM``)
* the city's own data contains ``THURDAY``, and an older format writes
  ``11:30AM TO 1PM MON & THURS``

The governing rule here is the one the whole product rests on: **never be
silently wrong**. Anything this cannot parse confidently returns ``None`` with
a reason, so it surfaces as a gap to fill rather than as a missing deadline
that looks like "nothing due today".
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import time

# ISO weekday numbers, as the rest of the app uses them.
MONDAY, TUESDAY, WEDNESDAY, THURSDAY, FRIDAY, SATURDAY, SUNDAY = range(1, 8)

_DAY_WORDS: dict[str, int] = {
    "MONDAY": MONDAY,
    "MON": MONDAY,
    "MONS": MONDAY,
    "TUESDAY": TUESDAY,
    "TUES": TUESDAY,
    "TUE": TUESDAY,
    "WEDNESDAY": WEDNESDAY,
    "WED": WEDNESDAY,
    "WEDS": WEDNESDAY,
    "THURSDAY": THURSDAY,
    "THURS": THURSDAY,
    "THUR": THURSDAY,
    "THU": THURSDAY,
    # The dataset contains this misspelling on real signs. Accepting it is not
    # sloppiness: refusing would silently drop rules for blocks that have them.
    "THURDAY": THURSDAY,
    "THURSDAYS": THURSDAY,
    "FRIDAY": FRIDAY,
    "FRI": FRIDAY,
    "FRIS": FRIDAY,
    "SATURDAY": SATURDAY,
    "SAT": SATURDAY,
    "SATS": SATURDAY,
    "SUNDAY": SUNDAY,
    "SUN": SUNDAY,
    "SUNS": SUNDAY,
}

# Bookkeeping about other sign designs, not part of the regulation.
_SUPERSEDE = re.compile(r"\(\s*SUPERSED(?:ES|ED\s+BY)[^)]*\)?", re.I)
# Pictogram descriptions. Removed before day parsing so that a "MOON & STARS"
# prefix cannot be mistaken for anything, but noted first — it marks an
# overnight rule.
_SYMBOLS = re.compile(r"\(?\s*(?:W/\s*)?\(?\s*MOON\s*[&/]\s*STARS?\s*(?:\(SYMBOLS?\))?\s*\)?", re.I)
_BROOM = re.compile(r"\(?\s*SANITATION\s+BROOM\s+SYMBOL\s*\)?", re.I)

_TIME = r"(?:MIDNIGHT|NOON|\d{1,2}\s*(?::\s*\d{2})?\s*[AP]\.?\s*M\.?)"
_RANGE = re.compile(rf"({_TIME})\s*(?:-|–|—|\bTO\b)\s*({_TIME})", re.I)

_SINGLE_TIME = re.compile(
    r"(?:(MIDNIGHT)|(NOON)|(\d{1,2})\s*(?::\s*(\d{2}))?\s*([AP])\.?\s*M\.?)", re.I
)


@dataclass(frozen=True)
class ParsedRule:
    days_of_week: tuple[int, ...]
    starts_at: time
    ends_at: time
    overnight_symbol: bool
    confidence: float
    raw: str

    @property
    def crosses_midnight(self) -> bool:
        return self.ends_at <= self.starts_at


class SignParseError(ValueError):
    """Carries why a sign could not be parsed, for reporting rather than
    swallowing."""


def _parse_time(token: str) -> time:
    m = _SINGLE_TIME.search(token)
    if not m:
        raise SignParseError(f"unrecognised time {token!r}")
    if m.group(1):
        return time(0, 0)
    if m.group(2):
        return time(12, 0)
    hour = int(m.group(3))
    minute = int(m.group(4) or 0)
    meridiem = m.group(5).upper()
    if hour == 12:
        hour = 0
    if meridiem == "P":
        hour += 12
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise SignParseError(f"impossible time {token!r}")
    return time(hour, minute)


def _parse_days(text: str) -> tuple[tuple[int, ...], float]:
    """Days the rule applies to, and how confident we are.

    ``EXCEPT SUNDAY`` names no days directly: it means every day but that one.
    """
    except_match = re.search(r"\bEXCEPT\s+([A-Z]+)", text, re.I)
    if except_match:
        excluded = _DAY_WORDS.get(except_match.group(1).upper())
        if excluded is None:
            raise SignParseError(f"unrecognised day after EXCEPT: {except_match.group(1)!r}")
        return tuple(d for d in range(1, 8) if d != excluded), 1.0

    found: list[int] = []
    for word in re.findall(r"[A-Z]+", text.upper()):
        day = _DAY_WORDS.get(word)
        if day is not None and day not in found:
            found.append(day)
    if not found:
        raise SignParseError("no days named")

    # An abbreviation or a known misspelling is still unambiguous here, so the
    # confidence stays high; it is the shape of the sentence we are unsure of,
    # not the day.
    return tuple(sorted(found)), 1.0


def parse_sign_description(description: str) -> ParsedRule:
    """Parse one ``sign_description``. Raises :class:`SignParseError` rather
    than guessing."""
    if not description or not description.strip():
        raise SignParseError("empty description")

    raw = description.strip()
    if not _BROOM.search(raw):
        raise SignParseError("not a street-cleaning sign")

    text = _SUPERSEDE.sub(" ", raw)
    overnight = bool(_SYMBOLS.search(text))
    text = _SYMBOLS.sub(" ", text)
    text = _BROOM.sub(" ", text)
    text = re.sub(r"\bNO\s+PARKING\b", " ", text, flags=re.I)
    # Arrows describe which way along the kerb the rule extends, not when.
    # The pattern requires at least one angle bracket: a looser "run of
    # hyphens" also matches the separator in "8:30AM-10AM" and silently
    # destroys every time range in the corpus.
    text = re.sub(r"(?:<+-*>*|<*-*>+)|W/\s*SINGLE\s+ARROW", " ", text)

    time_match = _RANGE.search(text)
    if not time_match:
        raise SignParseError("no time range")
    starts_at = _parse_time(time_match.group(1))
    ends_at = _parse_time(time_match.group(2))
    if starts_at == ends_at:
        raise SignParseError("zero-length window")

    # Days are named outside the time range; removing it first stops "10A M"
    # style typos leaving stray letters that look like day abbreviations.
    without_time = text[: time_match.start()] + " " + text[time_match.end() :]
    days, day_confidence = _parse_days(without_time)

    confidence = day_confidence
    if overnight and not (starts_at <= time(6, 0)):
        # The pictogram says overnight but the hours do not agree; trust the
        # hours and flag it.
        confidence = min(confidence, 0.7)

    return ParsedRule(
        days_of_week=days,
        starts_at=starts_at,
        ends_at=ends_at,
        overnight_symbol=overnight,
        confidence=confidence,
        raw=raw,
    )


def try_parse(description: str) -> tuple[ParsedRule | None, str | None]:
    """Non-raising form: ``(rule, None)`` or ``(None, reason)``."""
    try:
        return parse_sign_description(description), None
    except SignParseError as exc:
        return None, str(exc)
