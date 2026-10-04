"""Learn the shape of Turo's notification emails without reading them.

A parser needs to know which labels an email carries and in what order. It does
not need the values, and nobody should have to paste their mail into a chat
window to get a parser written — nor should that mail end up in a log that is
retained and readable by anyone with dashboard access.

So this reports structure and masks content. For each matching message it logs
the sender, a masked subject, and the ordered labels it found. Every value is
replaced by a token naming its type, so the output says

    label: trip starts -> <DATE> <TIME>

rather than the date, and

    subject: <NAME> booked your <NAME> for <DATE>

rather than the guest. That is enough to write and test a parser against, and
it stays true even as the values change.

Masking is deliberately over-eager: a capitalised word that is not part of
Turo's own vocabulary is treated as a name. Losing a label to over-masking
costs a round trip; leaking a guest's name into a log cannot be undone.

Links are the exception to "replace the whole value". A URL flattened to
``<URL>`` hides the one thing a parser wants from it — Turo puts a vehicle's id
in the path of the link behind its photo, and that id is the only unambiguous
way to tell two identical Corollas apart. So a URL keeps its host and its
lowercase route words and loses everything else, giving

    https://turo.com/us/en/vehicle-detail/<NUM>

which says where the id lives without saying what it is. Anything that is not a
plain lowercase word — a digit run, a hash, a base64 blob, an address with an
``@`` — is still a value and still goes.
"""

from __future__ import annotations

import base64
import html as html_module
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy.orm import Session

from turonomics_api.gmail.client import GmailClient, GmailError

log = logging.getLogger("turonomics.gmail.probe")

# ``from:turo``, not ``from:turo.com``. Turo sends booking mail from
# noreply@mail.turo.com, and Gmail did not match that subdomain against the
# apex domain — so the first run excluded every booking email and reported
# that none existed. A bare token matches any turo domain.
DEFAULT_QUERY = "from:turo newer_than:365d"

# High, because shapes are deduplicated before logging: a year of mail collapses
# to a handful of distinct shapes, so scanning widely costs little. The first
# run capped at 12 and logged "12 message(s) match", which read like a total and
# was really the cap being hit — the booking emails were simply older than the
# twelve newest.
DEFAULT_LIMIT = 150

# Gmail bills per-minute "query cost" units per user, and fetching messages as
# fast as httpx allows tripped it after about twenty-five. Pacing at five a
# second keeps a wide scan comfortably inside the limit, and costs half a minute
# on a run that happens once.
SECONDS_BETWEEN_FETCHES = 0.2
QUOTA_BACKOFF_SECONDS = 20.0

# Words that are structure rather than content, so they survive masking. Losing
# these would hide the labels the parser has to match on.
VOCABULARY = frozenset(
    # Split from a block rather than written as a list: the point is that it is
    # easy to add a word to when a label comes back over-masked.
    """
    turo trip trips booking booked reservation request requested confirmed
    cancelled canceled checkout check checkin in out start starts started
    end ends ended pickup pick up return returns returned drop off
    guest host vehicle car van total earnings payout fee fees tolls
    message messages sent reply you your my the a an for from to at on of and
    is are was were has have will reminder reminders upcoming today tomorrow
    location address delivery airport day days hour hours am pm
    id number no ref reference code
    view send reply earn earns earned mileage included miles note
    profile deposit payment business week once per within three days
    by about your has have question questions answers common concerns
    """.split()  # noqa: SIM905
)

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
_URL = re.compile(r"https?://\S+")
_MONEY = re.compile(r"\$\s?\d[\d,]*(?:\.\d{2})?")
_TIME = re.compile(r"\b\d{1,2}:\d{2}\s?(?:[AaPp]\.?[Mm]\.?)?\b|\b\d{1,2}\s?[AaPp]\.?[Mm]\.?\b")
_DATE = re.compile(
    r"\b\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?\b"
    r"|\b\d{4}-\d{2}-\d{2}\b"
    r"|\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2}(?:,?\s*\d{4})?\b"
    r"|\b(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]*,?\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2}\b",
    re.IGNORECASE,
)
_MODEL = re.compile(r"\b\d+[A-Za-z][\w-]*\b")
_PLATE = re.compile(r"\b[A-Z]{2,3}[- ]?\d{3,4}\b")
_NUM = re.compile(r"\b\d[\d,]{2,}\b")
_CAPS = re.compile(r"\b[A-Z][a-zA-Z'’]+\b")
_TOKEN = re.compile(r"<[A-Z]+(?: \d+w)?>")
# A stash marker: lowercase letters only, so that no later pass — not the
# model-number pass, not the capitalised-word pass — can see a value inside it.
_STASH = re.compile("\x00([a-z]+)\x00")
_LABEL = re.compile(r"^\s*([A-Za-z][A-Za-z \t'’&-]{1,40}?)\s*:\s*(.*)$")
# A shaped URL still has "https:" in it, so "Message Dana at https://…" reads
# as a label ending in "https" whose value is the rest of the link. Harmless,
# but it files a sentence under a label that does not exist and buries the real
# ones. Matched on the label's last word, not the whole of it.
_NOT_A_LABEL = frozenset({"http", "https", "mailto", "tel"})

_ANCHOR = re.compile(r"(?is)<a\b([^>]*)>(.*?)</a>")


def _attr(name: str) -> re.Pattern[str]:
    """Match one HTML attribute, quoted either way or not at all."""
    return re.compile(
        rf"""(?is)\b{name}\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s">]+))""",
    )


_HREF = _attr("href")
_ALT = _attr("alt")

# A path segment survives only if it is a plain lowercase route word. That rules
# out plates (LEH9892), ids (48812934), hashes, and the base64 blobs click
# trackers carry — those encode the recipient's own address often enough that
# keeping any of them would defeat the point of this module.
_SAFE_SEGMENT = re.compile(r"^[a-z][a-z-]{0,23}$")
_SAFE_KEY = re.compile(r"^[A-Za-z_][\w.-]{0,23}$")
_HEXISH = re.compile(r"^[0-9a-fA-F]{16,}$")

# Enough to see which links an email carries without the footer's worth of
# marketing and unsubscribe URLs drowning it.
MAX_LINKS = 20


def _stash_index(number: int) -> str:
    """A number written in letters, so a stash marker holds no digits."""
    return "".join(chr(ord("a") + int(digit)) for digit in str(number))


def _stash_number(letters: str) -> int:
    return int("".join(str(ord(char) - ord("a")) for char in letters))


def _stashed(value: str) -> bool:
    return bool(_STASH.fullmatch(value))


def _opaque(value: str) -> str:
    """Name the kind of a thing that is not a route word, without saying it."""
    if _stashed(value) or _TOKEN.fullmatch(value):
        # Already masked, from a second pass over logged output. Re-typing it
        # would turn <NUM> into <ID>, so a shape would change every time it was
        # re-read — and a shape you cannot quote back is not much of a record.
        return value
    if "@" in value:
        return "<EMAIL>"
    if value.isdigit():
        return "<NUM>"
    if _HEXISH.match(value):
        return "<HASH>"
    return "<ID>"


def url_shape(url: str) -> str:
    """A URL with its identifiers replaced and its route words kept.

    The route is the useful part: it says that a vehicle id lives at
    ``/us/en/vehicle-detail/<NUM>`` and a trip id at ``/trips/<NUM>/messages``.
    Everything that is not a plain lowercase word is a value — ids, hashes, the
    base64 payload a click tracker wraps the real link in — and goes.

    A scheme that is not http(s) keeps only its scheme: ``mailto:`` carries an
    address and ``tel:`` a phone number, so neither keeps anything else.
    """
    try:
        parts = urlsplit(html_module.unescape(url.strip()).rstrip(").,;\"'"))
    except ValueError:
        return "<URL>"
    scheme = (parts.scheme or "https").lower()
    if scheme not in {"http", "https"}:
        return f"{scheme}:<ID>"
    host = (parts.hostname or "").lower()
    if not host:
        return "<URL>"
    segments = [
        segment if _SAFE_SEGMENT.match(segment) else _opaque(segment)
        for segment in parts.path.split("/")
        if segment
    ]
    out = f"{scheme}://{host}"
    if segments:
        out += "/" + "/".join(segments)
    if parts.query:
        # Keys are structure — ``utm_source``, ``reservationId`` — and name what
        # the link takes. Values never are.
        keys = [pair.split("=", 1)[0] for pair in parts.query.split("&") if pair]
        shaped = [key if _SAFE_KEY.match(key) else "<ID>" for key in keys]
        out += "?" + "&".join(f"{key}=<VALUE>" for key in dict.fromkeys(shaped))
    if parts.fragment:
        out += "#<VALUE>"
    return out


def mask(text: str) -> str:
    """Replace values with type tokens, keeping structural words.

    Each finished token is parked in a stash and replaced by a marker of plain
    lowercase letters, then put back at the end. Writing ``<DATE>`` into the
    text would hand the capitalised-word pass a capitalised word to eat, turning
    every typed value back into ``<NAME>``; a marker with a digit in it gets
    eaten by the model-number pass instead. Both actually happened. Logs get
    re-read and shapes get quoted back, so masking has to be a fixed point.
    """
    stash: list[str] = []

    def unstash(text: str) -> str:
        return _STASH.sub(lambda m: stash[_stash_number(m.group(1))], text)

    def keep(token: str) -> str:
        # Resolved before it is stored, because a shaped URL can be built around
        # markers already in the text ("/trips/\x00b\x00/") and re.sub does not
        # rescan what it substitutes — so a nested marker would reach the output
        # verbatim. Earlier entries are already resolved, so this terminates.
        stash.append(unstash(token))
        return f"\x00{_stash_index(len(stash) - 1)}\x00"

    # Tokens already in the text come first, so a second pass leaves them be.
    out = _TOKEN.sub(lambda m: keep(m.group(0)), text)
    # Then URLs, by shape rather than by token, and whole: the route words a
    # shape keeps must not then be read as prose, nor its ids typed twice.
    out = _URL.sub(lambda m: keep(url_shape(m.group(0))), out)
    for kind, pattern in (
        ("EMAIL", _EMAIL),
        ("MONEY", _MONEY),
        ("DATE", _DATE),
        ("TIME", _TIME),
        ("PLATE", _PLATE),
        ("NAME", _MODEL),
        ("NUM", _NUM),
    ):
        out = pattern.sub(lambda _m, kind=kind: keep(f"<{kind}>"), out)

    def _word(m: re.Match[str]) -> str:
        word = m.group(0)
        return word if word.lower() in VOCABULARY else keep("<NAME>")

    out = _CAPS.sub(_word, out)
    out = unstash(out)
    # A run of the same token says no more than one of it, and reads worse. No
    # '/' in the separators: it would collapse "/<ID>/<ID>" in a URL shape to
    # one segment, which is the part of the shape worth having.
    out = re.sub(r"(<[A-Z]+>)(?:[\s,.;:|-]*\1)+", r"\1", out)
    return re.sub(r"[ \t]{2,}", " ", out).strip()


# More than a couple of words that are neither a token nor part of Turo's own
# vocabulary means the line is somebody's sentence rather than structure. The
# first version only asked whether a line contained *any* token, which let
# "all good, is that the address you want it back at on <NAME>?" through — the
# token came from masking "Tuesday", and the guest's message rode along with it.
# In a message notification the prose is the message, so this has to be the
# default rather than the exception.
MAX_FREE_WORDS = 2


def _is_structure(masked: str) -> bool:
    # URL shapes are discounted along with tokens: a shape is route words and
    # type tokens by construction, so it holds no prose to count. Counting it
    # made every line carrying a link read as somebody's sentence.
    bare = _TOKEN.sub(" ", _URL.sub(" ", masked))
    free = [
        word
        for word in re.findall(r"[A-Za-z][\w'’-]*", bare)
        if word.lower() not in VOCABULARY
    ]
    return len(free) <= MAX_FREE_WORDS


def _label_text(raw: str) -> str:
    """A label, masked and lowered, with its tokens left intact.

    Masking only the value was not enough: Turo writes "View Jenna's profile:",
    so the guest's name *is* part of the label. Lowercasing it did not help, and
    a first name in a retained log is exactly what this module exists to avoid.
    """
    masked = mask(raw.strip())
    lowered = masked.lower()
    for token in _TOKEN.findall(masked):
        lowered = lowered.replace(token.lower(), token)
    return lowered


@dataclass
class Shape:
    """What one email looks like, with nothing in it."""

    sender: str = ""
    subject: str = ""
    labels: list[str] = field(default_factory=list)
    lines: list[str] = field(default_factory=list)
    links: list[str] = field(default_factory=list)


def _header(payload: dict[str, Any], name: str) -> str:
    for h in payload.get("headers") or []:
        if h.get("name", "").lower() == name.lower():
            return str(h.get("value", ""))
    return ""


def _decode(data: str) -> str:
    pad = "=" * (-len(data) % 4)
    try:
        return base64.urlsafe_b64decode(data + pad).decode("utf-8", "replace")
    except Exception:  # noqa: BLE001 - a body we cannot decode is not fatal
        return ""


def plain_text(payload: dict[str, Any]) -> str:
    """The text/plain part, falling back to de-tagged HTML."""
    mime = payload.get("mimeType", "")
    body = (payload.get("body") or {}).get("data")
    if mime == "text/plain" and body:
        return _decode(body)
    for part in payload.get("parts") or []:
        found = plain_text(part)
        if found:
            return found
    if mime == "text/html" and body:
        html = _decode(body)
        html = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
        html = re.sub(r"(?i)<br\s*/?>|</(p|div|tr|li|h\d)>", "\n", html)
        return re.sub(r"<[^>]+>", " ", html)
    return ""


def html_of(payload: dict[str, Any]) -> str:
    """The text/html part, raw.

    :func:`plain_text` de-tags HTML, which throws the hrefs away — and the href
    is where the vehicle id is. So the links are read from the markup instead.
    """
    if payload.get("mimeType", "") == "text/html":
        body = (payload.get("body") or {}).get("data")
        if body:
            return _decode(body)
    for part in payload.get("parts") or []:
        found = html_of(part)
        if found:
            return found
    return ""


def _anchor_text(inner: str) -> str:
    """What the reader clicks: an image, or the masked words of the link.

    Named because the shape alone does not say which link is which. Turo hangs
    the vehicle link off the car's photo, so ``img`` versus text is how you tell
    that link from the half-dozen others pointing at the same route.
    """
    if re.search(r"(?i)<img\b", inner):
        found = _ALT.search(inner)
        alt = next((group for group in (found.groups() if found else ()) if group), "")
        return f"img[{mask(html_module.unescape(alt))}]" if alt.strip() else "img"
    text = mask(html_module.unescape(re.sub(r"<[^>]+>", " ", inner)))
    if not text:
        return "(empty)"
    if not _is_structure(text):
        return f"<PROSE {len(text.split())}w>"
    return text[:60]


def link_shapes(html: str) -> list[str]:
    """``descriptor -> url shape`` for each distinct link in an HTML body.

    Deduplicated, because a marketing footer repeats the same two links a dozen
    times and the question is which *kinds* of link an email carries.
    """
    found: list[str] = []
    for attrs, inner in _ANCHOR.findall(html):
        match = _HREF.search(attrs)
        if not match:
            continue
        href = next((group for group in match.groups() if group), "")
        if not href.strip():
            continue
        found.append(f"{_anchor_text(inner)} -> {url_shape(href)}")
    return list(dict.fromkeys(found))[:MAX_LINKS]


def shape_of(message: dict[str, Any]) -> Shape:
    payload = message.get("payload") or {}
    shape = Shape(
        sender=mask(_header(payload, "From")),
        subject=mask(_header(payload, "Subject")),
    )
    for raw in plain_text(payload).splitlines():
        # URLs first, before the line is split on a colon. "Message Dana at
        # https://turo.com/..." reads as label "message dana at https" with
        # "//turo.com/..." as its value, and a value that has already lost its
        # scheme no longer looks like a URL to the masker — so the link
        # survived. Masking is idempotent, so doing this early is free.
        line = _URL.sub(lambda m: url_shape(m.group(0)), raw.strip())
        if not line:
            continue
        matched = _LABEL.match(line)
        if matched and matched.group(1).split()[-1].lower() in _NOT_A_LABEL:
            matched = None
        if matched:
            shape.labels.append(
                f"{_label_text(matched.group(1))}: {mask(matched.group(2)) or '<EMPTY>'}"
            )
        elif len(shape.lines) < 12:
            masked = mask(line)
            shape.lines.append(masked if _is_structure(masked) else f"<PROSE {len(masked.split())}w>")
    shape.links = link_shapes(html_of(payload))
    return shape


def signature(shape: Shape) -> tuple[str, tuple[str, ...]]:
    """What makes two emails the same kind of email.

    The subject template plus the set of labels. Values differ between
    messages; the shape does not, which is the whole point.
    """
    return (shape.subject, tuple(entry.split(":", 1)[0] for entry in shape.labels))


def probe(
    session: Session, *, query: str = DEFAULT_QUERY, limit: int = DEFAULT_LIMIT
) -> list[Shape]:
    """Log the distinct shapes of recent Turo mail. Never raises.

    Deduplicated, because the question is "which kinds of email are there" and
    not "what arrived". Logging every message buried the answer and made a wide
    scan too noisy to run, which is how the first attempt ended up capped at
    twelve and missing the type that mattered.
    """
    shapes: list[Shape] = []
    try:
        client = GmailClient(session)
        ids = client.search(query, limit=limit)
        if len(ids) >= limit:
            log.warning(
                "probe: hit the cap of %d for %r — there are probably more, "
                "raise GMAIL_PROBE_LIMIT or narrow the query",
                limit,
                query,
            )
        log.info("probe: fetched %d message(s) for %r (cap %d)", len(ids), query, limit)

        seen: dict[tuple[str, tuple[str, ...]], int] = {}
        failures = 0
        for index, message_id in enumerate(ids):
            if index:
                time.sleep(SECONDS_BETWEEN_FETCHES)
            try:
                message = client.message(message_id)
            except GmailError as exc:
                # One unreadable message must not end the scan. The first
                # version wrapped the whole loop, so a single quota error threw
                # away everything already gathered — and because shapes were
                # only logged at the end, the twenty-five messages that had
                # succeeded were lost with it.
                failures += 1
                if "quota" in str(exc).lower() and failures == 1:
                    log.warning("quota hit after %d message(s); backing off", index)
                    time.sleep(QUOTA_BACKOFF_SECONDS)
                    continue
                log.warning("skipping a message: %s", exc)
                if failures > 5:
                    log.warning("giving up after %d failures; reporting what was read", failures)
                    break
                continue

            shape = shape_of(message)
            key = signature(shape)
            if key in seen:
                seen[key] += 1
                continue
            seen[key] = 1
            # Logged on discovery rather than at the end, so a run that dies
            # part way still tells you what it found.
            log.info("---- shape %d ----", len(seen))
            log.info("  from    : %s", shape.sender)
            log.info("  subject : %s", shape.subject)
            for label in shape.labels:
                log.info("  label   : %s", label)
            for line in shape.lines:
                log.info("  line    : %s", line)
            for link in shape.links:
                log.info("  link    : %s", link)
            shapes.append(shape)

        log.info(
            "probe: %d distinct shape(s) from %d message(s), %d unreadable",
            len(seen),
            len(ids) - failures,
            failures,
        )
        for (subject, _labels), count in sorted(seen.items(), key=lambda kv: -kv[1]):
            log.info("  %-3d message(s): %s", count, subject or "(no subject)")
    except GmailError as exc:
        log.warning("probe failed: %s", exc)
    except Exception as exc:  # noqa: BLE001 - diagnostics must not break boot
        log.warning("probe failed unexpectedly: %s", exc)
    return shapes
