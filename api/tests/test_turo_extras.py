"""Tests for a trip's photos and message thread, read off Turo's reservation page.

Shapes are the operator's probe of a past trip's page, values invented.
"""

from __future__ import annotations

from datetime import UTC, datetime
from datetime import timedelta as td
from typing import Any

import pytest

from turonomics_api.db.models import Trip, TripSource, TripState, Vehicle
from turonomics_api.ingest.turo_extras import (
    SETTLES_AFTER,
    ExtrasResult,
    apply_extras,
    photo_groups,
    thread,
    wanted_extras,
)

from .conftest import requires_db

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)


def _moment(when: datetime) -> dict[str, Any]:
    return {"epochMillis": int(when.timestamp() * 1000), "localDate": f"{when:%Y-%m-%d}",
            "localTime": f"{when:%H:%M}"}


def _photo(step: str, at: datetime, role: str = "GUEST") -> dict:
    return {"imageId": "x", "uuid": "y", "step": step, "photographerDriverRole": role,
            "takenAtTime": _moment(at), "createdTime": _moment(at),
            "createdByDriver": {"id": 1, "firstName": "Shashi"}, "imageType": None}


def _message(text: str | None, at: datetime, role: str = "GUEST", images: int = 0) -> dict:
    return {"author": {"id": 1, "firstName": "Shashi" if role == "GUEST" else "Host"},
            "authorDriverRole": role, "sentTime": _moment(at), "text": text,
            "media": {"images": [{"imageId": "i"}] * images}, "messageReadReceipts": []}


START = NOW - td(days=10)


def test_photos_are_counted_by_step_in_the_order_a_trip_happens() -> None:
    groups = photo_groups([
        _photo("RENTER_CHECK_OUT", START + td(days=3)),
        _photo("RENTER_CHECK_IN", START),
        _photo("RENTER_CHECK_IN", START + td(minutes=4)),
        _photo("TRIP_PHOTO", START + td(days=1), role="HOST"),
    ])
    assert [(g.step, g.count) for g in groups] == [
        ("RENTER_CHECK_IN", 2), ("TRIP_PHOTO", 1), ("RENTER_CHECK_OUT", 1),
    ]
    assert groups[0].first == START and groups[0].last == START + td(minutes=4)
    assert groups[1].by == "HOST"


def test_an_unknown_step_comes_last_rather_than_vanishing() -> None:
    groups = photo_groups([_photo("SOMETHING_NEW", START), _photo("RENTER_CHECK_IN", START)])
    assert [g.step for g in groups] == ["RENTER_CHECK_IN", "SOMETHING_NEW"]


def test_the_thread_reads_oldest_first_with_who_said_it() -> None:
    messages = thread([
        _message("Returned, keys in the box", START + td(days=3)),
        _message("Welcome!", START - td(hours=2), role="HOST"),
        _message(None, START + td(days=1), images=2),
    ])
    assert [m.text for m in messages] == ["Welcome!", None, "Returned, keys in the box"]
    assert messages[0].role == "HOST" and messages[0].name == "Host"
    assert messages[1].images == 2, "a photo with no words is still a message"


@pytest.fixture()
def car(session):
    vehicle = Vehicle(nickname="Jimmy", make="Toyota", model="Corolla", year=2024, plate="LWH4685")
    session.add(vehicle)
    session.flush()
    return vehicle


def _trip(session, car, rid: str, *, starts: datetime, ends: datetime,
          state: TripState = TripState.completed, synced: datetime | None = None) -> Trip:
    trip = Trip(vehicle_id=car.id, turo_trip_id=rid, starts_at=starts, ends_at=ends,
                state=state, source=TripSource.email, extras_synced_at=synced)
    session.add(trip)
    session.flush()
    return trip


@requires_db
def test_which_trips_are_read(session, car) -> None:
    """Started and not settled, never-read first. A trip read after it settled
    is history, and an upcoming one has nothing yet."""
    _trip(session, car, "never", starts=NOW - td(days=40), ends=NOW - td(days=38))
    _trip(session, car, "settled", starts=NOW - td(days=30), ends=NOW - td(days=28),
          synced=NOW - td(days=28) + SETTLES_AFTER + td(hours=1))
    _trip(session, car, "unsettled", starts=NOW - td(days=3), ends=NOW - td(days=1),
          synced=NOW - td(hours=6))
    _trip(session, car, "upcoming", starts=NOW + td(days=2), ends=NOW + td(days=4),
          state=TripState.upcoming)
    _trip(session, car, "cancelled", starts=NOW - td(days=5), ends=NOW - td(days=4),
          state=TripState.cancelled)
    assert wanted_extras(session, now=NOW) == ["never", "unsettled"]


@requires_db
def test_what_turo_returned_is_kept(session, car) -> None:
    trip = _trip(session, car, "1", starts=START, ends=START + td(days=3))
    result = ExtrasResult()
    apply_extras(session, "1", photos={"images": [_photo("TRIP_PHOTO", START)]},
                 messages=[_message("hi", START)], now=NOW, result=result)
    assert result.stored == 1
    assert trip.turo_photos is not None and len(trip.turo_photos) == 1
    assert trip.turo_messages is not None and trip.extras_synced_at == NOW


@requires_db
def test_a_failed_fetch_does_not_erase_what_was_kept(session, car) -> None:
    """None is "Turo did not answer", not "no photos"."""
    trip = _trip(session, car, "1", starts=START, ends=START + td(days=3))
    trip.turo_photos = [_photo("TRIP_PHOTO", START)]
    trip.turo_messages = [_message("hi", START)]
    result = ExtrasResult()
    apply_extras(session, "1", photos=None, messages={"error": "nope"}, now=NOW, result=result)
    assert result.stored == 0
    assert trip.turo_photos and trip.turo_messages
    assert trip.extras_synced_at is None, "not marked read, so the next pull tries again"


@requires_db
def test_an_unknown_reservation_is_reported(session, car) -> None:
    result = ExtrasResult()
    apply_extras(session, "nope", photos={"images": []}, messages=[], now=NOW, result=result)
    assert result.unknown == ["nope"]


@requires_db
def test_the_pull_and_the_view(api_client, monkeypatch, session, car) -> None:
    monkeypatch.delenv("TOLLS_TOKEN", raising=False)
    trip = _trip(session, car, "59077848", starts=START, ends=START + td(days=3))
    session.commit()
    wanted = api_client.get("/api/turo/wanted").json()
    assert wanted["extras"] == ["59077848"]
    assert wanted["photos_path"] == "/api/reservation/photos?reservationId={id}"
    assert wanted["messages_path"] == "/api/v2/reservation/conversation?reservationId={id}"

    view = api_client.get(f"/api/trips/{trip.id}/view").json()
    assert view["photos"] is None and view["messages"] is None, "not read is not none"

    out = api_client.post("/api/turo/extras", json={"items": [{
        "reservation_id": "59077848",
        "photos": {"images": [_photo("RENTER_CHECK_IN", START), _photo("RENTER_CHECK_IN", START)]},
        "messages": [_message("Here!", START)],
    }]}).json()
    assert out == {"stored": 1, "unknown": []}
    view = api_client.get(f"/api/trips/{trip.id}/view").json()
    assert view["photos"][0]["step"] == "RENTER_CHECK_IN" and view["photos"][0]["count"] == 2
    assert view["messages"][0]["text"] == "Here!"
    assert api_client.get("/api/turo/wanted").json()["extras"] == [], (
        "ended a week ago and read since it settled: not read again"
    )


@requires_db
def test_posting_extras_needs_the_token(api_client, monkeypatch) -> None:
    monkeypatch.setenv("TOLLS_TOKEN", "s3cret")
    assert api_client.post("/api/turo/extras", json={"items": []}).status_code == 401
