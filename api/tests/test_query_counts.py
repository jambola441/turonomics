"""The pages' two slow reads ask the database a fixed number of questions.

The ledger used to fetch each rental's crossings as it reached that rental,
and the trips list fetched each rental's toll count and reimbursement lines.
One round trip per row, to a database on another host, was the whole of the
pages' load time: four to six seconds of "loading…" on a fleet of four cars.

Fixed means: the number of statements at twelve rentals is the number at
three. That is the assertion, rather than an exact count, so a query added
for a good reason does not fail it while a query per row still does.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from turonomics_api.db.models import Toll, Trip, TripSource, TripState, Vehicle
from turonomics_api.routers.invoices import ledger
from turonomics_api.routers.trips import list_trips

from .conftest import requires_db

NOW = datetime(2026, 10, 25, 12, tzinfo=UTC)


def _rentals(session: Session, car: Vehicle, first: int, count: int) -> None:
    """`count` finished rentals, two crossings each, numbered from `first`."""
    for i in range(first, first + count):
        starts = NOW - timedelta(days=4 * i + 10)
        trip = Trip(
            vehicle_id=car.id,
            turo_trip_id=f"R{i}",
            guest_name=f"Guest {i}",
            starts_at=starts,
            ends_at=starts + timedelta(days=2),
            state=TripState.completed,
            source=TripSource.email,
        )
        session.add(trip)
        session.flush()
        for k in range(2):
            session.add(
                Toll(
                    fingerprint=f"t{i}-{k}",
                    occurred_at=starts + timedelta(hours=6 + k),
                    plaza="RKB",
                    amount_cents=746,
                    transponder_id="99900000111",
                    vehicle_id=car.id,
                    trip_id=trip.id,
                )
            )
    session.commit()


def _statements(engine: Engine, session: Session, read: Callable[[], object]) -> int:
    """How many statements `read` sends, from a session that remembers nothing."""
    # Expired, so that a lazy load the code relies on shows up as the query it
    # is, rather than being answered from what the fixture just inserted.
    session.expire_all()
    seen = 0

    def tick(*_: object) -> None:
        nonlocal seen
        seen += 1

    event.listen(engine, "before_cursor_execute", tick)
    try:
        read()
    finally:
        event.remove(engine, "before_cursor_execute", tick)
    return seen


@requires_db
def test_the_ledger_asks_the_same_questions_for_three_rentals_as_for_twelve(
    engine: Engine, session: Session
) -> None:
    car = Vehicle(nickname="Jerry", make="Toyota", model="Corolla", year=2025, plate="LZA7293")
    session.add(car)
    session.flush()
    _rentals(session, car, 0, 3)
    small = _statements(engine, session, lambda: ledger(session))
    _rentals(session, car, 3, 9)
    large = _statements(engine, session, lambda: ledger(session))
    assert len(ledger(session).rows) == 12, "every rental is on the ledger"
    assert large == small, f"{small} statements for 3 rentals became {large} for 12"


@requires_db
def test_the_trips_list_asks_the_same_questions_for_three_rentals_as_for_twelve(
    engine: Engine, session: Session, monkeypatch
) -> None:
    monkeypatch.delenv("TOLLS_TOKEN", raising=False)
    car = Vehicle(nickname="Jerry", make="Toyota", model="Corolla", year=2025, plate="LZA7293")
    session.add(car)
    session.flush()

    def read() -> object:
        return list_trips(session, manual_only=False, authorization=None)

    _rentals(session, car, 0, 3)
    small = _statements(engine, session, read)
    _rentals(session, car, 3, 9)
    large = _statements(engine, session, read)
    rows = list_trips(session, manual_only=False, authorization=None).trips
    assert len(rows) == 12 and all(r.toll_count == 2 for r in rows), "the counts still hold"
    assert large == small, f"{small} statements for 3 rentals became {large} for 12"
