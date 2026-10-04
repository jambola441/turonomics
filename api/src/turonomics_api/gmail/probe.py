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
"""

from __future__ import annotations

import base64
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

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
_PLATE = re.compile(r"\b[A-Z]{2,3}[- ]?\d{3,4}\b")
_NUM = re.compile(r"\b\d[\d,]{2,}\b")
_CAPS = re.compile(r"\b[A-Z][a-zA-Z'’]+\b")
# No '/' in a label: "Message Dana at https://..." would otherwise read as
# label "message dana at https" with the URL as its value, and the value
# would reach the masker already stripped of its scheme — so the URL regex
# would miss it and the link would survive. Keep URLs on the line path.
_TOKEN = re.compile(r"<[A-Z]+(?: \\d+w)?>")
_LABEL = re.compile(r"^\s*([A-Za-z][A-Za-z \t'’&-]{1,40}?)\s*:\s*(.*)$")


def mask(text: str) -> str:
    """Replace values with type tokens, keeping structural words.

    Substitutions go to lowercase sentinels first and become ``<TOKEN>`` only at
    the end. Writing ``<DATE>`` directly would hand the capitalised-word pass a
    capitalised word to eat, turning every typed value back into ``<NAME>`` —
    which is both useless to a parser and not stable under a second pass. Logs
    get re-read, so stability matters.
    """
    kinds = (
        ("url", _URL),
        ("email", _EMAIL),
        ("money", _MONEY),
        ("date", _DATE),
        ("time", _TIME),
        ("plate", _PLATE),
        ("num", _NUM),
    )
    out = text
    # Tokens already present (a second pass over logged output) become
    # sentinels too, so they are not re-masked.
    for kind, _ in (*kinds, ("name", None), ("empty", None)):
        out = out.replace(f"<{kind.upper()}>", f"\x00{kind}\x00")
    for kind, pattern in kinds:
        assert pattern is not None
        out = pattern.sub(f"\x00{kind}\x00", out)

    def _word(m: re.Match[str]) -> str:
        word = m.group(0)
        return word if word.lower() in VOCABULARY else "\x00name\x00"

    out = _CAPS.sub(_word, out)
    for kind, _ in (*kinds, ("name", None), ("empty", None)):
        out = out.replace(f"\x00{kind}\x00", f"<{kind.upper()}>")
    # A run of the same token says no more than one of it, and reads worse.
    out = re.sub(r"(<[A-Z]+>)(?:[\s,.;:/|-]*\1)+", r"\1", out)
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
    free = [
        word
        for word in re.findall(r"[A-Za-z][\w'’-]*", _TOKEN.sub(" ", masked))
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
        line = _URL.sub("<URL>", raw.strip())
        if not line:
            continue
        matched = _LABEL.match(line)
        if matched:
            shape.labels.append(
                f"{_label_text(matched.group(1))}: {mask(matched.group(2)) or '<EMPTY>'}"
            )
        elif len(shape.lines) < 12:
            masked = mask(line)
            shape.lines.append(masked if _is_structure(masked) else f"<PROSE {len(masked.split())}w>")
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
