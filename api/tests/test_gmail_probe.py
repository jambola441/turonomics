"""The probe reports structure and must not leak content.

The operator declined to hand over mailbox access or paste an email, which is a
reasonable call. This is the alternative: the app already has permission to read
the mail, so it reports the shape and masks the values. That only holds if the
masking actually works, so these tests assert on what must *not* survive — a
leak into a retained log cannot be undone, and "it looked fine" is not evidence.
"""

from __future__ import annotations

import base64

from turonomics_api.gmail.client import GmailError
from turonomics_api.gmail.probe import mask, plain_text, shape_of

# A realistic booking notification. Invented, but shaped like the real thing and
# seeded with every kind of value that must not come out the other side.
BODY = """\
Hi Pablo,

Dana Whitfield booked your Toyota 4Runner.

Trip ID: T-48812934
Guest: Dana Whitfield
Guest phone: (718) 555-0142
Vehicle: Toyota 4Runner 2023 (LEH9892)
Trip starts: Fri, Oct 10 at 10:00 AM
Trip ends: Mon, Oct 13 at 6:30 PM
Pickup location: 590 Bergen St, Brooklyn, NY 11238
Delivery fee: $34.28
Trip earnings: $412.60
Total: $446.88

Message Dana at https://turo.com/trips/48812934/messages
Questions? reply to support@turo.com
"""

SECRETS = (
    "Dana",
    "Whitfield",
    "Pablo",
    "48812934",
    "T-48812934",
    "555-0142",
    "LEH9892",
    "590 Bergen",
    "11238",
    "34.28",
    "412.60",
    "446.88",
    "Oct 10",
    "Oct 13",
    "10:00",
    "6:30",
    "support@turo.com",
    "turo.com/trips",
)


def _message(body: str, *, sender: str, subject: str) -> dict:
    encoded = base64.urlsafe_b64encode(body.encode()).decode().rstrip("=")
    return {
        "payload": {
            "mimeType": "text/plain",
            "headers": [
                {"name": "From", "value": sender},
                {"name": "Subject", "value": subject},
                {"name": "Date", "value": "Fri, 3 Oct 2026 14:02:11 -0400"},
            ],
            "body": {"data": encoded},
        }
    }


def _rendered(shape) -> str:
    return "\n".join([shape.sender, shape.subject, *shape.labels, *shape.lines])


def test_no_value_from_a_real_looking_email_survives():
    """The one test that matters. Everything else is convenience."""
    shape = shape_of(
        _message(
            BODY,
            sender="Turo <notifications@turo.com>",
            subject="Dana W. booked your Toyota 4Runner for Oct 10-13",
        )
    )
    rendered = _rendered(shape)
    leaked = [s for s in SECRETS if s in rendered]
    assert not leaked, f"masking leaked {leaked}\n--- output ---\n{rendered}"


def test_the_labels_survive_so_a_parser_can_be_written():
    """Masking that ate the labels too would be safe and useless."""
    shape = shape_of(
        _message(BODY, sender="Turo <notifications@turo.com>", subject="Trip booked")
    )
    labels = {entry.split(":", 1)[0] for entry in shape.labels}
    for expected in ("trip id", "guest", "vehicle", "trip starts", "trip ends", "total"):
        assert expected in labels, f"lost the {expected!r} label; labels were {sorted(labels)}"


def test_values_are_reported_by_type_not_erased():
    """A parser needs to know a field holds a date rather than that it held
    something."""
    shape = shape_of(_message(BODY, sender="Turo <x@turo.com>", subject="Trip booked"))
    by_label = dict(entry.split(":", 1) for entry in shape.labels)
    assert "<DATE>" in by_label["trip starts"] and "<TIME>" in by_label["trip starts"]
    assert "<MONEY>" in by_label["total"]
    assert "<NAME>" in by_label["guest"]


def test_the_sender_domain_survives_but_not_the_mailbox():
    """Which address Turo sends from is how the query gets narrowed; the local
    part is not needed for that."""
    shape = shape_of(
        _message(BODY, sender="Turo <notifications@turo.com>", subject="Trip booked")
    )
    assert "<EMAIL>" in shape.sender
    assert "notifications@turo.com" not in shape.sender


def test_an_html_only_email_still_yields_structure():
    """Turo sends HTML. A probe that only handled text/plain would report
    nothing and look like there was no mail."""
    html = (
        "<html><body><style>p{color:red}</style>"
        "<p>Guest: Dana Whitfield</p><p>Total: $446.88</p>"
        "</body></html>"
    )
    encoded = base64.urlsafe_b64encode(html.encode()).decode().rstrip("=")
    msg = {
        "payload": {
            "mimeType": "multipart/alternative",
            "headers": [{"name": "From", "value": "Turo <x@turo.com>"}],
            "parts": [{"mimeType": "text/html", "body": {"data": encoded}}],
        }
    }
    text = plain_text(msg["payload"])
    assert "Dana" in text, "the body must be extracted before it is masked"
    shape = shape_of(msg)
    rendered = _rendered(shape)
    assert "Dana" not in rendered and "Whitfield" not in rendered
    assert "color:red" not in rendered, "style content is not structure"
    assert any(e.startswith("guest:") for e in shape.labels)


def test_masking_is_idempotent():
    """The probe logs; logs get re-read and re-processed. Masking twice must not
    mangle the tokens it already wrote."""
    once = mask(BODY)
    assert mask(once) == once


def test_a_run_of_names_collapses():
    """'<NAME> <NAME> <NAME> <NAME>' is noise, not shape."""
    assert mask("Dana Whitfield Smith Jones booked") == "<NAME> booked"


# ---------------------------------------------------------------------------
# Leaks the first version shipped, found by running it against the real mailbox
# ---------------------------------------------------------------------------

# Verbatim shapes the probe reported, with the two leaks restored so the tests
# fail if either comes back. Turo puts the guest's name inside the label and the
# guest's own words in the body, neither of which the invented sample above had.
REAL_MESSAGE_NOTIFICATION = """\
Jenna has sent you a message about your Transit.

all good, is that the address you want it back at on Tuesday?

Reply https://turo.com/trips/12345/messages

Trip start: Oct 5 10:00 AM
Trip end: Oct 8 4:00 PM
You earn: $284.00
Mileage included: 600 miles
View Jenna's profile: https://turo.com/drivers/9876
Send Jenna a message: https://turo.com/trips/12345/messages

Ford Transit 2024
booked by Jenna
Jenna Alvarez
(917) 555-0188
Reservation ID #12345
"""


def test_a_name_inside_a_label_does_not_leak():
    """The first leak. Only the value was masked, so "View Jenna's profile:"
    put a guest's first name in the log — lowercased, which helped nothing.
    """
    shape = shape_of(
        _message(
            REAL_MESSAGE_NOTIFICATION,
            sender="Turo <noreply@turo.com>",
            subject="Jenna has sent you a message about your Transit",
        )
    )
    rendered = _rendered(shape).lower()
    assert "jenna" not in rendered, f"a name leaked via a label:\n{_rendered(shape)}"
    assert "alvarez" not in rendered


def test_the_guests_own_words_do_not_leak():
    """The second leak. Masking capitalised words left lowercase prose intact,
    and in a message notification the prose is the message."""
    shape = shape_of(
        _message(REAL_MESSAGE_NOTIFICATION, sender="Turo <x@turo.com>", subject="Message")
    )
    rendered = _rendered(shape)
    for phrase in ("address you want it back at", "all good"):
        assert phrase not in rendered, f"guest message text leaked: {phrase!r}"
    assert any("<PROSE" in line for line in shape.lines), "prose should be reported as prose"


def test_masking_the_label_keeps_it_recognisable():
    """A label masked into uselessness is safe and worthless; the parser has to
    be able to tell these fields apart."""
    shape = shape_of(
        _message(REAL_MESSAGE_NOTIFICATION, sender="Turo <x@turo.com>", subject="Message")
    )
    labels = {entry.split(":", 1)[0] for entry in shape.labels}
    assert "trip start" in labels
    assert "trip end" in labels
    assert "you earn" in labels
    assert "mileage included" in labels
    # The two interpolated-name labels survive as distinguishable shapes: the
    # name is gone but "view ... profile" and "send ... a message" are not.
    assert "view <NAME> profile" in labels
    assert "send <NAME> a message" in labels


def test_structural_lines_survive_the_prose_filter():
    """A line with a token in it is structure and must be kept — the
    reservation id and the vehicle line are how a trip gets identified."""
    shape = shape_of(
        _message(REAL_MESSAGE_NOTIFICATION, sender="Turo <x@turo.com>", subject="Message")
    )
    joined = "\n".join(shape.lines)
    assert "Reservation ID #<NUM>" in joined
    assert "booked by <NAME>" in joined


# ---------------------------------------------------------------------------
# Why the first run missed the booking emails
# ---------------------------------------------------------------------------


def test_two_messages_of_the_same_kind_report_as_one_shape():
    """The first run logged every message and so had to cap at twelve, which
    hid the email type that mattered. Deduplicating is what makes a wide scan
    cheap enough to actually find every kind."""
    from turonomics_api.gmail.probe import signature

    a = shape_of(_message(REAL_MESSAGE_NOTIFICATION, sender="Turo <x@mail.turo.com>",
                          subject="Jenna has sent you a message about your Transit"))
    b = shape_of(_message(
        REAL_MESSAGE_NOTIFICATION.replace("Jenna", "Marcus").replace("$284.00", "$512.40"),
        sender="Turo <x@mail.turo.com>",
        subject="Marcus has sent you a message about your Transit",
    ))
    assert signature(a) == signature(b), "different guests, same kind of email"


def test_a_different_kind_of_email_is_a_different_shape():
    payout = "Ka-ching! Turo sent your earnings payment of $284.00.\nNote: we deposit weekly.\n"
    a = shape_of(_message(REAL_MESSAGE_NOTIFICATION, sender="Turo <x@mail.turo.com>",
                          subject="Jenna has sent you a message about your Transit"))
    b = shape_of(_message(payout, sender="Turo <x@mail.turo.com>",
                          subject="Your earnings are on the way!"))
    from turonomics_api.gmail.probe import signature

    assert signature(a) != signature(b)


def test_the_default_query_matches_turos_sending_subdomain():
    """The miss that started this: booking mail comes from mail.turo.com, and
    'from:turo.com' did not match it, so the first run reported that no booking
    email existed."""
    from turonomics_api.gmail.probe import DEFAULT_QUERY

    assert "from:turo.com" not in DEFAULT_QUERY, "an apex-only filter misses mail.turo.com"
    assert "from:turo" in DEFAULT_QUERY


# ---------------------------------------------------------------------------
# Surviving the quota, which the first wide run did not
# ---------------------------------------------------------------------------


class _FakeClient:
    """Stands in for GmailClient. Fails on whichever fetches you nominate."""

    def __init__(self, bodies: list[str], *, fail_at: set[int], error: str):
        self.bodies = bodies
        self.fail_at = fail_at
        self.error = error
        self.fetched = 0

    def search(self, query: str, *, limit: int = 0) -> list[str]:
        return [str(i) for i in range(len(self.bodies))]

    def message(self, message_id: str) -> dict:
        index = int(message_id)
        self.fetched += 1
        if index in self.fail_at:
            raise GmailError(self.error)
        return _message(self.bodies[index], sender="Turo <x@mail.turo.com>", subject=f"S{index}")


def _probe_with(client, monkeypatch):
    import turonomics_api.gmail.probe as mod

    monkeypatch.setattr(mod, "GmailClient", lambda session: client)
    monkeypatch.setattr(mod, "SECONDS_BETWEEN_FETCHES", 0)
    monkeypatch.setattr(mod, "QUOTA_BACKOFF_SECONDS", 0)
    return mod.probe(None)


def test_a_quota_error_does_not_discard_what_was_already_read(monkeypatch):
    """What happened live: Gmail's per-minute quota tripped after about
    twenty-five messages, the exception escaped the loop, and because shapes
    were only logged at the end, every message already read was lost with it.
    """
    bodies = [f"Guest: Person{i}\nTotal: ${i}.00\n" for i in range(6)]
    client = _FakeClient(
        bodies,
        fail_at={3},
        error="GET /users/me/messages/x failed (403): Quota exceeded for quota metric",
    )
    shapes = _probe_with(client, monkeypatch)

    assert client.fetched == 6, "the scan must continue past the quota error"
    assert shapes, "shapes read before the error must survive it"


def test_it_gives_up_rather_than_grinding_through_a_broken_mailbox(monkeypatch):
    """Skipping forever would turn a systemic failure into a slow one."""
    bodies = [f"Guest: Person{i}\n" for i in range(40)]
    client = _FakeClient(bodies, fail_at=set(range(40)), error="GET x failed (500): boom")
    assert _probe_with(client, monkeypatch) == []
    assert client.fetched < 40, "must stop once failures are clearly systemic"
