"""Turn a Turo notification email into a trip record.

Built against the shapes in docs/design/02-turo-email-shapes.md, observed from
the real mailbox rather than guessed at. Three of those observations do most of
the work here:

* The **reservation id** is the only stable key, and it lives in the body as
  ``Reservation ID #12345`` — not in a header or the subject. Every email in a
  trip's life carries it, which is what lets a cancellation find the booking it
  cancels.
* The **year** is in the prose, not the labels. ``Trip start:`` gives a date and
  time with no year; the "Ka-ching!" sentence spells out the full range. Parsing
  the sentence means the year never has to be guessed, so the bug where a
  December booking for January lands eleven months in the past cannot happen.
  When only the labels are present the year is inferred, and the record says so.
* **No email carries a timezone.** These are the vehicle's local time. That is
  an assumption, so it is applied explicitly from the fleet timezone and
  recorded, rather than being silently baked in.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from turonomics_api.db.models import TripState
from turonomics_api.gmail.links import listing_id

# What kind of event the email reports. The subject is what distinguishes them;
# the body block is near-identical across the trip-bearing ones.
BOOKED = "booked"
CHANGED = "changed"
CHANGE_REQUESTED = "change_requested"
CANCELLED = "cancelled"
UPCOMING = "upcoming"
ENDING = "ending"
MESSAGE = "message"
RATED = "rated"
LICENCE = "licence"
PAYOUT = "payout"
REIMBURSEMENT = "reimbursement"
UNKNOWN = "unknown"

# Ordered: the first match wins, so the more specific patterns come first.
# "has requested a change" must beat "has changed", and "confirmed ... change
# request" must beat both.
_SUBJECT_KINDS: tuple[tuple[str, str], ...] = (
    (r"has requested a change to their trip", CHANGE_REQUESTED),
    (r"confirmed .*change request", CHANGED),
    (r"has changed their trip", CHANGED),
    (r"has added another driver", CHANGED),
    (r"\bis booked\b", BOOKED),
    (r"has canceled their trip|has cancelled their trip", CANCELLED),
    (r"has an upcoming trip", UPCOMING),
    (r"ends tomorrow", ENDING),
    (r"has sent you a message", MESSAGE),
    (r"just rated their trip", RATED),
    (r"confirm your guest.s license", LICENCE),
    (r"earnings are on the way", PAYOUT),
    (r"reimbursement invoice", REIMBURSEMENT),
)

# Which kinds carry a usable trip record. A payout has no reservation id, and a
# rating arrives after the trip is over.
TRIP_BEARING = frozenset({BOOKED, CHANGED, CHANGE_REQUESTED, CANCELLED, UPCOMING, ENDING, MESSAGE})

STATE_FOR_KIND = {
    BOOKED: TripState.upcoming,
    CHANGED: TripState.upcoming,
    CHANGE_REQUESTED: TripState.upcoming,
    UPCOMING: TripState.upcoming,
    ENDING: TripState.active,
    MESSAGE: TripState.upcoming,
    CANCELLED: TripState.cancelled,
}

_RESERVATION = re.compile(r"Reservation\s+ID\s*#\s*(\d+)", re.IGNORECASE)
_BOOKED_BY = re.compile(r"^[ \t]*(?:booked|requested)\s+by\s+(.+?)[ \t]*$",
                        re.IGNORECASE | re.MULTILINE)
_EARNINGS = re.compile(r"You\s+earn:?\s*\$\s?([\d,]+(?:\.\d{2})?)", re.IGNORECASE)
_LABEL_START = re.compile(r"^[ \t]*Trip\s+start:[ \t]*(.+?)[ \t]*$",
                          re.IGNORECASE | re.MULTILINE)
_LABEL_END = re.compile(r"^[ \t]*Trip\s+end:[ \t]*(.+?)[ \t]*$",
                        re.IGNORECASE | re.MULTILINE)

# "...is booked from Oct 5, 2026, 10:00 AM to Oct 8, 2026, 4:00 PM."
_STAMP = (
    r"[A-Z][a-z]{2,8}\.?\s+\d{1,2}(?:st|nd|rd|th)?,?\s*\d{4},?"
    r"(?:\s+at)?\s*\d{1,2}(?::\d{2})?\s*[AP]\.?M\.?"
)
_RANGE = re.compile(
    rf"from\s+(?P<from>{_STAMP})\s+to\s+(?P<to>{_STAMP})", re.IGNORECASE
)

# A make/model/year line: "Ford Transit 2024", "Toyota 4Runner 2023".
_VEHICLE_LINE = re.compile(
    r"^[ \t]*([A-Z][A-Za-z-]+(?:\s+[A-Za-z0-9][\w-]*){1,3})\s+(19|20)\d{2}[ \t]*$",
    re.MULTILINE,
)

_WITH_YOUR = re.compile(r"with your\s+(.+?)(?:\s+is\b|\s*\(|[.!]|$)", re.IGNORECASE)


def _tidy(text: str) -> str:
    """Normalise a date string enough for strptime.

    Turo writes "Oct 5, 2026, 10:00 AM" in one place and "Oct 5 at 10 AM" in
    another, and a non-breaking space turns up wherever the HTML had one.
    """
    # U+202F (narrow no-break space) is what Turo puts before AM/PM; U+00A0 and
    # U+2009 turn up elsewhere, and \r survives CRLF line endings because "$"
    # in multiline mode matches before the \n, not before the \r.
    cleaned = re.sub(r"[\u00a0\u2009\u202f\r]", " ", text).replace(",", " ")
    cleaned = re.sub(r"\b(\d{1,2})(?:st|nd|rd|th)\b", r"\1", cleaned)
    cleaned = re.sub(r"\bat\b", " ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\.", "", cleaned)
    return " ".join(cleaned.split())


class ParseError(ValueError):
    """The email did not contain a usable trip."""


@dataclass(frozen=True)
class ParsedTrip:
    kind: str
    reservation_id: str
    guest_name: str | None
    starts_at: datetime
    ends_at: datetime
    state: TripState
    vehicle_text: str | None
    # Turo's listing id, from the link behind the car's photo. Present on every
    # trip-bearing email type; None when the message was plain text or linked
    # more than one car. This is what resolves two cars of the same model.
    turo_listing_id: str | None
    earnings_cents: int | None
    # True when the year came from the prose range rather than being inferred.
    year_was_explicit: bool
    # The zone the naive email times were read in — an assumption, recorded.
    assumed_timezone: str


def classify(subject: str) -> str:
    for pattern, kind in _SUBJECT_KINDS:
        if re.search(pattern, subject, re.IGNORECASE):
            return kind
    return UNKNOWN


def _parse_stamp(text: str, tz: ZoneInfo) -> datetime:
    cleaned = _tidy(text)
    for fmt in ("%b %d %Y %I:%M %p", "%B %d %Y %I:%M %p", "%b %d %Y %I %p", "%B %d %Y %I %p"):
        try:
            return datetime.strptime(cleaned, fmt).replace(tzinfo=tz)
        except ValueError:
            continue
    raise ParseError(f"unparseable timestamp: {text!r}")


# Label dates that carry their own year. The live mailbox writes "10/2/26 8:00 AM"
# — numeric, two-digit year — which the first version of this did not expect at
# all, having been written against a hand-typed "Oct 5 10:00 AM".
_DATED_FORMATS = (
    "%m/%d/%y %I:%M %p",
    "%m/%d/%Y %I:%M %p",
    "%m/%d/%y %I %p",
    "%m/%d/%Y %I %p",
    "%m/%d/%y %H:%M",
    "%m/%d/%Y %H:%M",
    "%b %d %Y %I:%M %p",
    "%B %d %Y %I:%M %p",
)

# Label dates that do not, and have to be placed against the email's arrival.
_UNDATED_FORMATS = (
    "%b %d %I:%M %p",
    "%B %d %I:%M %p",
    "%b %d %I %p",
    "%B %d %I %p",
    "%b %d %H:%M",
    "%B %d %H:%M",
)


def _parse_label(text: str, *, received: datetime, tz: ZoneInfo) -> tuple[datetime, bool]:
    """A ``Trip start:`` value, and whether it told us the year itself.

    Year-bearing formats are tried first, because a year that was stated beats
    one that was deduced — and the live mailbox does state it.

    When it does not, the nearest candidate to the email's arrival wins. Turo
    sends these days or weeks ahead, never years, so trying the arrival year
    first and stepping forward would put a December email about a January trip
    eleven months in the past.
    """
    cleaned = _tidy(text)
    for fmt in _DATED_FORMATS:
        try:
            return datetime.strptime(cleaned, fmt).replace(tzinfo=tz), True
        except ValueError:
            continue
    for fmt in _UNDATED_FORMATS:
        try:
            bare = datetime.strptime(cleaned, fmt)
        except ValueError:
            continue
        candidates = [
            bare.replace(year=year, tzinfo=tz)
            for year in (received.year - 1, received.year, received.year + 1)
        ]
        return min(candidates, key=lambda when: abs(when - received)), False
    raise ParseError(f"unparseable label date: {text!r}")


def parse_email(
    *,
    subject: str,
    body: str,
    received_at: datetime,
    fleet_timezone: str = "America/New_York",
    html: str | None = None,
) -> ParsedTrip:
    """Parse one Turo notification. Raises ParseError when it carries no trip.

    ``html`` is the raw markup, if the message had any. The body text alone
    names the car as "Toyota Corolla 2025", which is not an identifier; the
    link behind the car's photo is. Optional because a plain-text message still
    parses — it just falls back to matching on words.
    """
    kind = classify(subject)
    if kind not in TRIP_BEARING:
        raise ParseError(f"{kind} emails carry no trip record")

    # Normalise line endings once rather than teaching every line-anchored
    # pattern about \r. "$" in multiline mode matches before the \n, so on CRLF
    # mail every captured value keeps a trailing carriage return — which is
    # invisible in a log and rejected by strptime.
    body = body.replace("\r\n", "\n").replace("\r", "\n")

    reservation = _RESERVATION.search(body)
    if not reservation:
        # Without the key there is nothing to attach the trip to, and inventing
        # one would create a duplicate on the next email about the same trip.
        raise ParseError("no reservation id")

    tz = ZoneInfo(fleet_timezone)

    explicit = _RANGE.search(body)
    if explicit:
        starts_at = _parse_stamp(explicit.group("from"), tz)
        ends_at = _parse_stamp(explicit.group("to"), tz)
        year_was_explicit = True
    else:
        start_label = _LABEL_START.search(body)
        end_label = _LABEL_END.search(body)
        if not (start_label and end_label):
            raise ParseError("no trip dates")
        starts_at, start_dated = _parse_label(
            start_label.group(1), received=received_at, tz=tz
        )
        ends_at, end_dated = _parse_label(end_label.group(1), received=received_at, tz=tz)
        year_was_explicit = start_dated and end_dated

    if ends_at <= starts_at:
        # A trip crossing New Year with inferred years lands here: the end was
        # resolved into the same year as the start. Push it forward rather than
        # storing an interval the database will reject.
        if not year_was_explicit:
            ends_at = ends_at.replace(year=ends_at.year + 1)
        if ends_at <= starts_at:
            raise ParseError(f"trip ends before it starts: {starts_at} -> {ends_at}")

    guest = _BOOKED_BY.search(body)
    earnings = _EARNINGS.search(body)
    # Prefer the body's "make model year" line; fall back to the subject's
    # "with your <vehicle>", which omits the year but is always present.
    vehicle_text: str | None = None
    vehicle = _VEHICLE_LINE.search(body)
    if vehicle:
        vehicle_text = " ".join(vehicle.group(0).split())
    else:
        from_subject = _WITH_YOUR.search(subject)
        if from_subject:
            vehicle_text = from_subject.group(1).strip()

    return ParsedTrip(
        kind=kind,
        reservation_id=reservation.group(1),
        guest_name=guest.group(1).strip() if guest else None,
        starts_at=starts_at,
        ends_at=ends_at,
        state=STATE_FOR_KIND[kind],
        vehicle_text=vehicle_text,
        turo_listing_id=listing_id(html) if html else None,
        earnings_cents=(
            int(round(float(earnings.group(1).replace(",", "")) * 100)) if earnings else None
        ),
        year_was_explicit=year_was_explicit,
        assumed_timezone=fleet_timezone,
    )


def is_active(trip: ParsedTrip, *, now: datetime) -> bool:
    """Whether the trip is under way, ignoring what the email called it.

    An "upcoming trip" reminder for a trip that has since started should not
    reset its state to upcoming; the dates are more trustworthy than the label.
    """
    return trip.starts_at <= now < trip.ends_at + timedelta(0)
