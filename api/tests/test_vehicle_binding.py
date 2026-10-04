"""Resolving which car an email is about when the words cannot.

This fleet runs two Toyota Corollas. The body of a Turo email names a car as
"Toyota Corolla 2025", so the word score ties, and ``match_vehicle`` refuses to
guess — correctly, because a street-cleaning alert rides on the answer. On the
live mailbox that refusal dropped 20 of every 40 messages.

The link behind the car's photo carries Turo's own id for the listing. These
tests are about using it, and about the two ways using it could go wrong:
binding an id to the wrong car, and quietly stealing one that is already bound.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from turonomics_api.db.models import Vehicle
from turonomics_api.ingest.trips import match_vehicle

from .conftest import requires_db

pytestmark = requires_db

JERRY_LISTING = "12345678"
JOLENE_LISTING = "22222222"
JIMMY_LISTING = "87654321"


@pytest.fixture()
def fleet(session):
    rows = [
        Vehicle(nickname="Jerry", make="Toyota", model="Corolla", year=2025),
        Vehicle(nickname="Jolene", make="Toyota", model="Corolla", year=2025),
        Vehicle(nickname="Jimmy", make="Toyota", model="4-Runner", year=2023),
        Vehicle(nickname="Bubba", make="Ford", model="Transit", year=2018),
    ]
    session.add_all(rows)
    session.commit()
    return {v.nickname: v for v in rows}


def _by(session, nickname: str) -> Vehicle:
    return session.scalars(select(Vehicle).where(Vehicle.nickname == nickname)).one()


def test_two_corollas_still_tie_on_words_alone(session, fleet):
    """The behaviour this exists to fix. Kept as a test because refusing to
    guess remains the right answer when there is no link."""
    assert match_vehicle(session, "Toyota Corolla 2025") is None


def test_a_bound_listing_resolves_the_tie(session, fleet):
    fleet["Jerry"].turo_listing_id = JERRY_LISTING
    session.commit()
    matched = match_vehicle(session, "Toyota Corolla 2025", listing_id=JERRY_LISTING)
    assert matched is not None and matched.nickname == "Jerry"


def test_each_corolla_resolves_to_its_own_listing(session, fleet):
    """The whole point: same words, different cars."""
    fleet["Jerry"].turo_listing_id = JERRY_LISTING
    fleet["Jolene"].turo_listing_id = JOLENE_LISTING
    session.commit()
    assert match_vehicle(session, "Toyota Corolla 2025", listing_id=JERRY_LISTING).nickname == (
        "Jerry"
    )
    assert match_vehicle(session, "Toyota Corolla 2025", listing_id=JOLENE_LISTING).nickname == (
        "Jolene"
    )


def test_the_listing_outranks_the_words(session, fleet):
    """If Turo's own id and the free text disagree, the id wins. The text is a
    description someone typed; the id is what Turo thinks the car is."""
    fleet["Jolene"].turo_listing_id = JOLENE_LISTING
    session.commit()
    matched = match_vehicle(session, "Ford Transit 2018", listing_id=JOLENE_LISTING)
    assert matched.nickname == "Jolene"


def test_an_unambiguous_car_teaches_itself_its_listing(session, fleet):
    """Jimmy is the only 4-Runner, so the words already resolve him. Recording
    the id on the way past means the fuzzy path is never needed again — and it
    costs nothing, because this branch only runs when exactly one car matched.
    """
    matched = match_vehicle(session, "Toyota 4-Runner 2023", listing_id=JIMMY_LISTING)
    session.commit()
    assert matched.nickname == "Jimmy"
    assert _by(session, "Jimmy").turo_listing_id == JIMMY_LISTING


def test_learning_never_steals_a_listing_from_another_car(session, fleet):
    """If the words point one way and an existing binding points another, the
    binding wins and nothing is rebound. Overwriting would move a car's whole
    trip history onto a different vehicle.

    It holds because the listing lookup runs first and returns early, not
    because of a check in the learning branch — a first draft had such a check
    and mutation testing proved it unreachable. This test is what keeps the
    early return from being "simplified" away.
    """
    fleet["Bubba"].turo_listing_id = JIMMY_LISTING
    session.commit()
    matched = match_vehicle(session, "Toyota 4-Runner 2023", listing_id=JIMMY_LISTING)
    assert matched.nickname == "Bubba", "the binding wins over the words"
    assert _by(session, "Jimmy").turo_listing_id is None


def test_an_ambiguous_match_does_not_learn(session, fleet):
    """A tie is not evidence. Binding the id to either Corolla here would be
    the coin flip the tie exists to prevent."""
    assert match_vehicle(session, "Toyota Corolla 2025", listing_id=JERRY_LISTING) is None
    session.commit()
    assert _by(session, "Jerry").turo_listing_id is None
    assert _by(session, "Jolene").turo_listing_id is None


def test_an_unclaimed_listing_is_named_in_the_log(session, fleet, caplog):
    """A log that says "ambiguous" without saying which listing is a log you
    cannot act on. The id is the operator's own and is in the URL of their own
    Turo page, so it is printed rather than masked."""
    with caplog.at_level("INFO", logger="turonomics.ingest.trips"):
        match_vehicle(session, "Toyota Corolla 2025", listing_id=JERRY_LISTING)
    assert JERRY_LISTING in caplog.text
    assert "--turo-listing" in caplog.text, "say how to fix it, not just that it is broken"


def test_an_unknown_listing_falls_back_to_the_words(session, fleet):
    """A car whose listing has never been seen must still match by name, or
    binding would have to happen before anything worked at all."""
    matched = match_vehicle(session, "Ford Transit 2018", listing_id="99999999")
    assert matched.nickname == "Bubba"


def test_no_listing_and_no_text_matches_nothing(session, fleet):
    assert match_vehicle(session, None) is None
    assert match_vehicle(session, "") is None
