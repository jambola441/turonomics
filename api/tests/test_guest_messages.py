"""Keeping what a guest actually wrote, beside the car it is about.

This is the one place in the mail pipeline that stores a guest's prose instead
of masking it, so the tests carry that boundary explicitly: the app keeps the
words, the probe still must not let them near a log.

The reason for the feature is a real afternoon: a guest messaged to say which
side of Prospect Place she had left the car on, the operator read it on their
phone, and then told the app separately — because the app had read the same
email, used it to close the trip, and thrown the sentence away.
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from turonomics_api.db.models import GuestMessage, Vehicle
from turonomics_api.gmail.parse import MAX_MESSAGE_CHARS, guest_message, parse_email
from turonomics_api.gmail.probe import shape_of
from turonomics_api.ingest.mail import sync_trips_from_mail

from .conftest import requires_db

NOW = datetime(2026, 10, 4, 20, 40, tzinfo=UTC)

# The shape the probe reported from the live mailbox: a header line, the
# guest's own words, then "Reply <url>" and the labels.
NOTE = """\
Cambria has sent you a message about your Corolla.

Parked on the north side of Prospect Place, just past the hydrant.

Reply https://turo.com/us/en/reservation/900101/messages

Trip start: Oct 1 2:00 PM
Trip end: Oct 4 4:00 PM
You earn: $284.00
View Cambria's profile: https://turo.com/us/en/drivers/9876
Toyota Corolla 2025
booked by Cambria
Reservation ID #900101
"""


def test_the_guests_words_come_out_whole():
    body, guest = guest_message(NOTE)
    assert body == "Parked on the north side of Prospect Place, just past the hydrant."
    assert guest == "Cambria"


def test_paragraph_breaks_survive_but_layout_does_not():
    text, _ = guest_message(
        "Dana has sent you a message about your Transit.\n\n"
        "Running about 30 min late.\n\n\n\n"
        "Also the tank is full.\n\n"
        "Reply https://turo.com/x\n"
    )
    assert text == "Running about 30 min late.\n\nAlso the tank is full."


def test_a_colon_in_the_message_does_not_truncate_it():
    """A label-shaped delimiter would cut this at the first word. Truncating
    what the person actually wrote, to be tidy, is the wrong trade."""
    text, _ = guest_message(
        "Dana has sent you a message about your Transit.\n\n"
        "Note: left the keys in the lockbox.\n\n"
        "Reply https://turo.com/x\n"
    )
    assert text == "Note: left the keys in the lockbox."


def test_the_turo_boilerplate_is_not_part_of_the_message():
    text, _ = guest_message(NOTE)
    for boilerplate in ("Reply", "Trip start", "Reservation ID", "turo.com", "You earn"):
        assert boilerplate not in text


def test_other_kinds_of_email_carry_no_message():
    assert guest_message("Your earnings are on the way!\n\nKa-ching!\n") == (None, None)
    assert guest_message("") == (None, None)


def test_a_runaway_parse_cannot_store_an_entire_email():
    text, _ = guest_message(
        "Dana has sent you a message about your Transit.\n\n" + ("x" * 9000)
    )
    assert len(text) == MAX_MESSAGE_CHARS


def test_parse_email_attaches_the_message_only_to_message_notifications():
    parsed = parse_email(
        subject="Cambria has sent you a message about your Corolla",
        body=NOTE,
        received_at=NOW,
    )
    assert parsed.guest_message == (
        "Parked on the north side of Prospect Place, just past the hydrant."
    )

    # The decisive case: a body that still carries the header sentence, on an
    # email the subject classifies as a booking. Only the kind check can reject
    # this — replacing the sentence too would let the regex do the work and the
    # guard would go untested.
    assert (
        parse_email(
            subject="Cambria's trip with your Corolla is booked!",
            body=(
                "Ka-ching! Cambria's trip with your Toyota Corolla is booked from "
                "Oct 1, 2026, 2:00 PM to Oct 4, 2026, 4:00 PM.\n\n" + NOTE
            ),
            received_at=NOW,
        ).guest_message
        is None
    ), "classification decides, not the presence of the sentence"


# ---------------------------------------------------------------------------
# The boundary: the app keeps it, the probe still must not
# ---------------------------------------------------------------------------


def test_the_probe_still_refuses_to_log_what_the_app_now_stores():
    """The two postures are deliberate and opposite, and that only holds if
    this stays true. The probe writes to a retained log readable by anyone with
    dashboard access; the app shows the operator their own mail."""
    encoded = base64.urlsafe_b64encode(NOTE.encode()).decode().rstrip("=")
    shape = shape_of(
        {
            "payload": {
                "mimeType": "text/plain",
                "headers": [
                    {"name": "From", "value": "Turo <noreply@mail.turo.com>"},
                    {
                        "name": "Subject",
                        "value": "Cambria has sent you a message about your Corolla",
                    },
                ],
                "body": {"data": encoded},
            }
        }
    )
    rendered = "\n".join([shape.subject, *shape.labels, *shape.lines, *shape.links])
    for secret in ("Cambria", "hydrant", "Prospect"):
        assert secret not in rendered, f"{secret!r} leaked into a probe shape"


# ---------------------------------------------------------------------------
# Through the sync
# ---------------------------------------------------------------------------


class FakeGmail:
    def __init__(self, messages):
        self.messages = messages

    def search(self, query: str, *, limit: int = 0) -> list[str]:
        return [str(i) for i in range(len(self.messages))]

    def message(self, message_id: str) -> dict:
        subject, body = self.messages[int(message_id)]
        return {
            "internalDate": str(int(NOW.timestamp() * 1000)),
            "payload": {
                "mimeType": "text/plain",
                "headers": [{"name": "Subject", "value": subject}],
                "body": {
                    "data": base64.urlsafe_b64encode(body.encode()).decode().rstrip("=")
                },
            },
        }


SUBJECT = "Cambria has sent you a message about your Corolla"


@requires_db
def test_the_sync_stores_the_note_against_the_car(session):
    session.add(Vehicle(nickname="Jolene", make="Toyota", model="Corolla", year=2025))
    session.commit()
    sync_trips_from_mail(session, client=FakeGmail([(SUBJECT, NOTE)]), now=NOW)
    session.commit()

    note = session.scalars(select(GuestMessage)).one()
    assert note.guest_name == "Cambria"
    assert "north side of Prospect Place" in note.body
    assert note.vehicle.nickname == "Jolene"
    assert note.trip_id is not None, "the note is tied to the trip it is about"


@requires_db
def test_re_reading_the_same_email_does_not_duplicate_the_note(session):
    """The sync re-reads the same seven-day window every ten minutes. Without
    the Gmail id as a key this stores the same sentence 144 times a day."""
    session.add(Vehicle(nickname="Jolene", make="Toyota", model="Corolla", year=2025))
    session.commit()
    client = FakeGmail([(SUBJECT, NOTE)])
    for _ in range(3):
        sync_trips_from_mail(session, client=client, now=NOW)
        session.commit()
    assert len(session.scalars(select(GuestMessage)).all()) == 1


@requires_db
def test_a_note_for_an_unmatched_car_is_not_stored_loose(session):
    """GuestMessage.vehicle_id is required — a note nobody can attribute is
    worse than no note, because it would show on whichever car you opened."""
    sync_trips_from_mail(session, client=FakeGmail([(SUBJECT, NOTE)]), now=NOW)
    session.commit()
    assert session.scalars(select(GuestMessage)).all() == []


@requires_db
def test_the_fleet_view_serves_recent_notes_newest_first(session, api_client):
    car = Vehicle(nickname="Jolene", make="Toyota", model="Corolla", year=2025)
    session.add(car)
    session.flush()
    for days, text in ((0, "newest"), (1, "middle"), (10, "ancient")):
        session.add(
            GuestMessage(
                vehicle_id=car.id,
                gmail_message_id=f"m{days}",
                guest_name="Cambria",
                body=text,
                received_at=datetime.now(UTC) - timedelta(days=days),
            )
        )
    session.commit()

    notes = api_client.get("/api/fleet").json()["vehicles"][0]["guest_notes"]
    assert [n["body"] for n in notes] == ["newest", "middle"], "two most recent, newest first"


@requires_db
def test_an_old_note_is_dropped_even_when_there_is_room_for_it(session, api_client):
    """The cap alone would hide this one behind newer notes, so the window has
    to be tested where nothing else can exclude it. A guest saying "parked on
    the north side" ten days ago next to a parking session from this afternoon
    is not context, it is a confident wrong answer."""
    car = Vehicle(nickname="Jolene", make="Toyota", model="Corolla", year=2025)
    session.add(car)
    session.flush()
    session.add(
        GuestMessage(
            vehicle_id=car.id,
            gmail_message_id="old",
            guest_name="Cambria",
            body="parked on the north side",
            received_at=datetime.now(UTC) - timedelta(days=10),
        )
    )
    session.commit()
    assert api_client.get("/api/fleet").json()["vehicles"][0]["guest_notes"] == []
