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
    # The guest's own words, on a message notification only. Stored and shown
    # rather than masked: the operator reading what their guest actually wrote,
    # beside the car it is about, is why this exists.
    guest_message: str | None
    # Turo's listing id, from the link behind the car's photo. Present on every
    # trip-bearing email type; None when the message was plain text or linked
    # more than one car. This is what resolves two cars of the same model.
    turo_listing_id: str | None
    earnings_cents: int | None
    # True when the year came from the prose range rather than being inferred.
    year_was_explicit: bool
    # The zone the naive email times were read in — an assumption, recorded.
    assumed_timezone: str


# "Jenna has sent you a message about your Transit." — the line the guest's own
# words follow. Captured from the body rather than the subject because the
# subject is truncated on long names.
_MESSAGE_HEADER = re.compile(
    r"^(?P<guest>.+?) has sent you a message about your\b.*$", re.IGNORECASE | re.MULTILINE
)

# What ends the quoted message. In every observed example the guest's words are
# followed by "Reply https://turo.com/...", and every label in the email comes
# after that — so a URL is the delimiter, and the labels never need to be one.
#
# Matching labels too would be stricter and worse: a guest writing "Note: the
# tank is full" would have their message cut at the first word. Truncating the
# person's actual words to be tidy is the wrong trade.
_MESSAGE_END = re.compile(r"https?://|^Reservation ID #", re.IGNORECASE)

# Long enough for anything a guest types into a phone, short enough that a
# malformed parse cannot put a whole email body in the database.
MAX_MESSAGE_CHARS = 2000


def guest_message(body: str) -> tuple[str | None, str | None]:
    """The guest's own words from a message notification, and who sent them.

    Returns ``(None, None)`` for every other kind of email. The text is kept
    verbatim — this is the one place in the mail pipeline that stores a guest's
    prose rather than masking it, because showing it to the operator beside the
    car it is about is the entire point. The probe's masking exists so that
    *logs* never carry it; this is the operator's own app showing the operator
    their own mail.
    """
    body = body.replace("\r\n", "\n").replace("\r", "\n")
    header = _MESSAGE_HEADER.search(body)
    if header is None:
        return None, None

    kept: list[str] = []
    for line in body[header.end() :].split("\n")[1:]:
        if _MESSAGE_END.search(line.strip()):
            break
        kept.append(line.rstrip())

    # Leading and trailing blank lines are layout; blank lines in the middle are
    # the guest's own paragraph breaks and are kept.
    text = "\n".join(kept).strip()
    text = re.sub(r"\n{3,}", "\n\n", text)
    guest = (header.group("guest") or "").strip() or None
    return (text[:MAX_MESSAGE_CHARS] or None), guest


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
        guest_message=guest_message(body)[0] if kind == MESSAGE else None,
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

# ---------------------------------------------------------------------------
# Reimbursement invoices
# ---------------------------------------------------------------------------
# A reimbursement invoice is not a trip: no dates, no reservation block, so
# parse_email rightly refuses it. But it is the record of money already asked
# for, and without it the tolls page asks for the same money twice — which is
# worse than not asking, because the guest has already paid it once.
#
# Three subjects, which are three states of the same invoice. All of them carry
# the reservation in a link and the amount in a "Total charge" line.

INVOICE_FILED = "filed"
INVOICE_CHARGED = "charged"
INVOICE_UNANSWERED = "unanswered"

_INVOICE_SUBJECTS: tuple[tuple[str, str], ...] = (
    # Order matters: "has not responded to your reimbursement invoice" also
    # contains "reimbursement invoice".
    (r"has not responded to your reimbursement invoice", INVOICE_UNANSWERED),
    (r"has been charged for your reimbursement invoice", INVOICE_CHARGED),
    (r"\binvoice\b", INVOICE_FILED),
)

# turo.com/reservation/<id>/receipt, and .../invoice-hub?invoiceId=<id>
_INVOICE_RESERVATION = re.compile(
    r"turo\.com/(?:[a-z]{2}/[a-z]{2}/)?reservation/(\d{4,})(?:/|\b)", re.IGNORECASE
)
_INVOICE_ID = re.compile(r"invoiceId=([A-Za-z0-9_-]{4,})", re.IGNORECASE)
_TOTAL_CHARGE = re.compile(r"Total\s+charge\s*[-–—:]\s*\$?\s*([\d,]+\.\d{2})", re.IGNORECASE)

# "Tolls - $16.79", one per charge under the invoice's "Incidental charges"
# heading. Matching the total was not enough: of eight charged invoices on the
# live account, not one total equalled the rental's tolls, because a
# reimbursement bundles cleaning, fuel and damage onto the same invoice. The
# toll line is the part that can be reconciled.
#
# The label may *begin* with a digit. Turo writes the quantity first —
# "22 mi additional distance - $11.00", "7 tolls - $40.71" — so a pattern
# anchored on a leading letter silently dropped every quantified line. On the
# live account that was three of the eight charged invoices: the plain labels
# ("Tolls", "Tickets", "Refueling") matched all along.
#
# Said wrongly once and worth stating correctly: a sync that read 149 invoices
# and stored line items for none of them is *not* evidence of this bug. That
# run predated line items being parsed at all. This bug was found by reading
# one invoice whose only charge was quantified, and seeing nothing.
#
# The first version of this was written against a guess at the format
# ("Additional mileage (120 mi)"), and the guess parsed while the real thing
# did not. The amount is still anchored to the end of its own line, which is
# what keeps the sentence underneath each charge out.
_LINE_ITEM = re.compile(
    r"^\s*([A-Za-z0-9][A-Za-z0-9 /&'.,()+-]{1,60}?)\s*[-–—]\s*\$\s*([\d,]+\.\d{2})\s*$",
    re.MULTILINE,
)

# What Turo might call the toll line. Deliberately loose on the label and strict
# about everything else: a label this does not recognise means no line matched,
# which falls back to the total and changes nothing.
_TOLL_LABEL = re.compile(r"\btolls?\b", re.IGNORECASE)

# The other things a reimbursement invoice charges for. Not parsed in order to
# reconcile them — nothing here knows what the mileage or the fuel should have
# been — but to recognise a line that is about more than tolls. "Tolls and
# fuel - $55.55" names two charges, and taking the whole amount as tolls would
# write off the fuel as though a guest had paid it.
# Each word earns its place by one test: when it sits *beside* "toll" in a
# label, does the amount stop being toll-only? That is the only thing this
# pattern ever does, since a label without "toll" in it is not a candidate
# anyway.
#
# "fee" and "fees" failed that test and were removed. "Toll fees - $15.55" is
# plainly the toll line, and refusing it would leave money uncollected for the
# sake of a word — whereas "Tolls and fuel" genuinely does not say what the
# toll share was.
_OTHER_CHARGE = re.compile(
    r"\b(?:mileage|miles|fuel|gas|petrol|ticket|tickets|citation|citations|"
    r"violation|violations|cleaning|smoking|damage|overage|distance|pet|"
    r"delivery|parking)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ParsedInvoice:
    """A reimbursement invoice as the notification describes it."""

    state: str
    reservation_id: str
    guest_name: str | None
    total_cents: int
    # Turo's own id, where the link carries one. The "charged" notification
    # links the receipt rather than the invoice hub, so it often does not.
    turo_invoice_id: str | None
    # Every charge on the invoice, as (label, cents). Stored as well as used,
    # because the labels are Turo's words and nobody here can see them
    # otherwise — the masked probe reports them as <NAME>.
    lines: tuple[tuple[str, int], ...] = ()

    @property
    def toll_cents(self) -> int | None:
        """What this invoice charged for tolls alone, if it says so plainly.

        None in three cases, all of which fall back to comparing the total and
        so refuse rather than guess:

        * no line is recognisable as tolls — the common case for an invoice
          that is entirely cleaning, damage or a ticket;
        * a line names tolls *and* something else ("Tolls and fuel"), where the
          toll share is not stated and taking the whole amount would write off
          the fuel as though the guest had paid it;
        * more than one line names tolls, which is not a shape seen in the wild
          and not one to improvise on.
        """
        found = [
            cents
            for label, cents in self.lines
            if _TOLL_LABEL.search(label) and not _OTHER_CHARGE.search(label)
        ]
        return found[0] if len(found) == 1 else None

    @property
    def fingerprint(self) -> str:
        """One identity across the three notifications of one invoice.

        Turo's invoice id when a link carries it. Otherwise the reservation and
        the amount, which is what the three emails have in common — keyed this
        way so "filed", "unanswered" and "charged" collapse onto one record
        rather than counting as three invoices for the same money.
        """
        if self.turo_invoice_id:
            return f"inv:{self.turo_invoice_id}"
        return f"res:{self.reservation_id}:{self.total_cents}"


def names_tolls(label: str) -> bool:
    """Whether a charge label is about tolls at all.

    Public because the recovery rule needs a distinction `toll_cents` cannot
    make: it is None both for an invoice that charged no tolls and for one
    whose toll line is unreadable ("Tolls and fuel", or two toll lines). The
    first charged nothing and must never cover a crossing; the second charged
    something unknown and has to be looked at by a person.
    """
    return bool(_TOLL_LABEL.search(label))


def classify_invoice(subject: str) -> str | None:
    for pattern, state in _INVOICE_SUBJECTS:
        if re.search(pattern, subject, re.IGNORECASE):
            return state
    return None


def parse_invoice(subject: str, body: str, html: str | None = None) -> ParsedInvoice | None:
    """A reimbursement invoice, or None if this email is not one.

    None rather than raising: most Turo mail is not an invoice, and the caller
    is a loop over everything in the mailbox.
    """
    state = classify_invoice(subject)
    if state is None:
        return None
    haystack = f"{body}\n{html or ''}"
    reservation = _INVOICE_RESERVATION.search(haystack)
    total = _TOTAL_CHARGE.search(body)
    if reservation is None or total is None:
        return None
    invoice_id = _INVOICE_ID.search(haystack)
    lines = tuple(
        (label.strip(), int(round(float(amount.replace(",", "")) * 100)))
        for label, amount in _LINE_ITEM.findall(body)
        # The total is not one of the charges it totals.
        if not re.fullmatch(r"total\s+charge", label.strip(), re.IGNORECASE)
    )
    return ParsedInvoice(
        lines=lines,
        state=state,
        reservation_id=reservation.group(1),
        guest_name=_guest_from_subject(subject),
        total_cents=int(round(float(total.group(1).replace(",", "")) * 100)),
        turo_invoice_id=invoice_id.group(1) if invoice_id else None,
    )


def _guest_from_subject(subject: str) -> str | None:
    """"Dylan has been charged for your reimbursement invoice" -> "Dylan".

    Only for display beside the matched rental; the reservation id is what
    actually identifies it.
    """
    match = re.match(r"\s*([^\d]{1,60}?)\s+has (?:been charged|not responded)", subject)
    return match.group(1).strip() or None if match else None
