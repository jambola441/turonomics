"""Finding the car in a Turo email, from the link behind its photo.

Every URL here is a real shape, reported by the probe against the live mailbox
and recorded in docs/design/02-turo-email-shapes.md. The ids are invented; the
paths are not. This module has been written against guessed formats twice
before and rejected every message in the mailbox both times.
"""

from __future__ import annotations

import pytest

from turonomics_api.gmail.links import listing_id, listing_ids

# The two real shapes, from shapes 2 and 6 of the probe run.
COROLLA = "https://turo.com/us/en/car-rental/united-states/brooklyn-ny/toyota/corolla/12345678"
FOURRUNNER = "https://turo.com/us/en/suv-rental/united-states/brooklyn-ny/toyota/4runner/87654321"
YOUR_CAR = "https://turo.com/your-car/5551234"
# Also in the same emails, also numeric, neither a car.
DRIVER = "https://turo.com/us/en/drivers/9876543"
RESERVATION = "https://turo.com/us/en/reservation/4455667/messages"


def _anchor(href: str, inner: str = "<img src=x>") -> str:
    return f'<a href="{href}">{inner}</a>'


@pytest.mark.parametrize(
    ("url", "expected"),
    [(COROLLA, "12345678"), (FOURRUNNER, "87654321"), (YOUR_CAR, "5551234")],
)
def test_the_listing_id_is_the_last_segment(url, expected):
    assert listing_id(_anchor(url)) == expected


@pytest.mark.parametrize("url", [DRIVER, RESERVATION])
def test_a_guest_or_a_reservation_is_not_a_car(url):
    """The decisive test. These sit in the same email and are the same shape of
    number; taking "the last number in a turo.com link" would attach every trip
    to one imaginary vehicle."""
    assert listing_ids(_anchor(url)) == []


def test_the_car_is_found_among_the_other_links():
    """A real booking email carries the guest's profile, the reservation, app
    store badges and a map, all before or after the one link that matters."""
    html = (
        _anchor(DRIVER, "Dana")
        + _anchor(RESERVATION, "Reply")
        + _anchor(COROLLA)
        + _anchor("https://turo.com/help", "Help")
    )
    assert listing_id(html) == "12345678"


def test_two_cars_in_one_email_is_not_an_answer():
    """Returning the first would be inventing certainty. Falling back to the
    fuzzy word match is strictly better than being confidently wrong about
    which guest has which car."""
    other = COROLLA.replace("12345678", "22222222")
    assert listing_id(_anchor(COROLLA) + _anchor(other)) is None
    assert listing_ids(_anchor(COROLLA) + _anchor(other)) == ["12345678", "22222222"]


def test_the_same_car_linked_twice_is_still_one_answer():
    """Turo links the car from both its photo and its name."""
    assert listing_id(_anchor(COROLLA) + _anchor(COROLLA, "2025 Toyota Corolla")) == "12345678"


def test_html_entities_in_the_href_are_decoded():
    """Turo writes &amp; in hrefs, and a query string is normal on these."""
    tracked = f"{COROLLA}?utm_source=email&amp;utm_medium=booking"
    assert listing_id(_anchor(tracked)) == "12345678"


@pytest.mark.parametrize("quote", ["'", '"', ""])
def test_the_href_is_found_however_it_is_quoted(quote):
    assert listing_id(f"<a href={quote}{COROLLA}{quote}><img></a>") == "12345678"


def test_a_plain_text_email_has_no_links():
    """Not an error. The parser falls back to matching on words."""
    assert listing_id("Dana booked your Toyota Corolla 2025.") is None
    assert listing_id("") is None


def test_a_body_type_this_fleet_does_not_own_yet_still_resolves():
    """car-rental and suv-rental are the two observed. The segment varies with
    the car, so a van must not need a code change to be recognised."""
    van = "https://turo.com/us/en/minivan-rental/united-states/brooklyn-ny/ford/transit/777888"
    assert listing_id(_anchor(van)) == "777888"


def test_a_listing_that_moves_city_still_resolves():
    """The path carries country and city. Binding to them would mean a listing
    stops matching the day its address changes."""
    moved = COROLLA.replace("brooklyn-ny", "jersey-city-nj")
    assert listing_id(_anchor(moved)) == "12345678"
