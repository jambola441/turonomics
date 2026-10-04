"""Attributing EZPass crossings to the guest who was driving.

Two failure modes here cost real money and both are quiet. Double-counting on
re-import inflates what you think you are owed; silently dropping a toll
nobody can be billed for shrinks it. The tests are mostly about those.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from turonomics_api.db.models import Toll, Trip, TripSource, TripState, Vehicle
from turonomics_api.ingest.tolls import import_tolls, rematch_unattributed

from .conftest import requires_db

pytestmark = requires_db

NOW = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)

HEADER = "Lane Txn ID,Tag/Plate #,Agency,Entry Plaza,Exit Plaza,Class,Date,Exit Time,Amount"


def _csv(*rows: str) -> str:
    return HEADER + "\n" + "\n".join(rows) + "\n"


def _row(txn: str, tag_or_plate: str, date: str, time: str, amount: str, plaza: str = "RKB") -> str:
    return f'{txn},{tag_or_plate},MTAB&T,,{plaza},31,{date},{time},${amount}'


@pytest.fixture()
def jerry(session):
    car = Vehicle(nickname="Jerry", make="Toyota", model="Corolla", year=2025, plate="LZA7293")
    session.add(car)
    session.flush()
    return car


def _trip(session, car, *, guest, starts, ends, state=TripState.completed) -> Trip:
    trip = Trip(
        vehicle_id=car.id,
        turo_trip_id=f"R{int(starts.timestamp())}",
        guest_name=guest,
        starts_at=starts,
        ends_at=ends,
        state=state,
        source=TripSource.email,
    )
    session.add(trip)
    session.flush()
    return trip


# ---------------------------------------------------------------------------
# The thing it exists to do
# ---------------------------------------------------------------------------


def test_a_toll_lands_on_the_guest_who_was_driving(session, jerry):
    _trip(session, jerry, guest="Jenna",
          starts=NOW - timedelta(days=1), ends=NOW + timedelta(days=1))
    result = import_tolls(
        session, _csv(_row("33237138399", "NY LZA7293", "10/04/2026", "11:10:36 AM", "-9.11"))
    )
    session.commit()
    assert (result.matched, result.unmatched) == (1, 0)
    toll = session.scalars(select(Toll)).one()
    assert toll.vehicle.nickname == "Jerry"
    assert toll.trip.guest_name == "Jenna"
    assert toll.amount_cents == 911


def test_a_toll_outside_any_trip_belongs_to_the_owner(session, jerry):
    """The car was yours that day. Attributed to the vehicle, not to a guest —
    billing somebody for a crossing they did not make is worse than eating it.
    """
    _trip(session, jerry, guest="Jenna",
          starts=NOW - timedelta(days=9), ends=NOW - timedelta(days=8))
    result = import_tolls(
        session, _csv(_row("1", "NY LZA7293", "10/04/2026", "11:10:36 AM", "-9.11"))
    )
    session.commit()
    toll = session.scalars(select(Toll)).one()
    assert result.unmatched == 1
    assert toll.vehicle.nickname == "Jerry", "whose car it is, is known"
    assert toll.trip_id is None, "who was driving, is not"


def test_overlapping_trips_give_the_toll_to_the_narrower_window(session, jerry):
    """Back-to-back rentals overlap by minutes around a handover. The shorter
    trip is the more specific claim."""
    _trip(session, jerry, guest="Wide",
          starts=NOW - timedelta(hours=6), ends=NOW + timedelta(hours=6))
    _trip(session, jerry, guest="Narrow",
          starts=NOW - timedelta(minutes=30), ends=NOW + timedelta(minutes=30))
    import_tolls(session, _csv(_row("2", "NY LZA7293", "10/04/2026", "12:00:00 PM", "-6.94")))
    session.commit()
    assert session.scalars(select(Toll)).one().trip.guest_name == "Narrow"


# ---------------------------------------------------------------------------
# Re-import, which is the expensive mistake
# ---------------------------------------------------------------------------


def test_importing_the_same_statement_twice_does_not_double_the_bill(session, jerry):
    """A reconciliation tool that inflates on re-upload is worse than none."""
    statement = _csv(
        _row("33237138399", "NY LZA7293", "10/04/2026", "11:10:36 AM", "-9.11"),
        _row("33237138400", "NY LZA7293", "10/04/2026", "01:22:00 PM", "-6.94"),
    )
    first = import_tolls(session, statement)
    session.commit()
    second = import_tolls(session, statement)
    session.commit()
    assert (first.imported, second.imported) == (2, 0)
    assert second.already_known == 2
    assert len(session.scalars(select(Toll)).all()) == 2


def test_an_export_without_a_transaction_id_still_deduplicates(session, jerry):
    """Not every export has the id column. The crossing itself is the fallback
    key, or re-importing one of those files silently doubles it."""
    no_id = (
        "Tag/Plate #,Agency,Entry Plaza,Exit Plaza,Class,Date,Exit Time,Amount\n"
        "NY LZA7293,MTAB&T,,RKB,31,10/04/2026,11:10:36 AM,$-9.11\n"
    )
    import_tolls(session, no_id)
    session.commit()
    again = import_tolls(session, no_id)
    session.commit()
    assert again.already_known == 1
    assert len(session.scalars(select(Toll)).all()) == 1


# ---------------------------------------------------------------------------
# Transponders, which is most of a real statement
# ---------------------------------------------------------------------------


def test_a_tag_read_toll_matches_once_the_tag_is_bound(session, jerry):
    """Plate rows only happen when the tag failed. The normal case names a
    transponder and nothing else, so without the binding most of a statement is
    unattributable."""
    _trip(session, jerry, guest="Jenna",
          starts=NOW - timedelta(days=1), ends=NOW + timedelta(days=1))
    statement = _csv(_row("3", " 00414500433", "10/04/2026", "11:13:32 AM", "-2.86", plaza="19"))

    loose = import_tolls(session, statement)
    session.commit()
    assert loose.unmatched == 1
    assert loose.unknown_tags == {"00414500433"}, "named, so it can be bound"

    jerry.ezpass_tag = "00414500433"
    session.commit()
    assert rematch_unattributed(session) == 1
    session.commit()

    toll = session.scalars(select(Toll)).one()
    assert toll.vehicle.nickname == "Jerry"
    assert toll.trip.guest_name == "Jenna"


def test_rematching_is_needed_because_re_importing_would_not_help(session, jerry):
    """The rows are already known, so the importer skips them. Without a
    rematch the binding would appear to do nothing."""
    _trip(session, jerry, guest="Jenna",
          starts=NOW - timedelta(days=1), ends=NOW + timedelta(days=1))
    statement = _csv(_row("4", " 00414500433", "10/04/2026", "11:13:32 AM", "-2.86", plaza="19"))
    import_tolls(session, statement)
    session.commit()

    jerry.ezpass_tag = "00414500433"
    session.commit()
    again = import_tolls(session, statement)
    session.commit()
    assert again.imported == 0 and again.already_known == 1
    assert session.scalars(select(Toll)).one().trip_id is None, "re-import changed nothing"

    assert rematch_unattributed(session) == 1


# ---------------------------------------------------------------------------
# What must not be swallowed
# ---------------------------------------------------------------------------


def test_a_toll_for_a_car_outside_the_fleet_is_kept_not_dropped(session, jerry):
    """A personal car on the same account. Dropping it would make the ledger
    disagree with the statement, which is how nobody trusts the ledger."""
    import_tolls(session, _csv(_row("5", "NY ZZZ9999", "10/04/2026", "09:00:00 AM", "-4.00")))
    session.commit()
    toll = session.scalars(select(Toll)).one()
    assert toll.vehicle_id is None and toll.trip_id is None
    assert toll.license_plate == "ZZZ9999", "so it can be recognised"


def test_payments_and_credits_are_not_tolls(session, jerry):
    """A statement carries account top-ups. Counting one as a toll owed would
    be money invented out of nothing."""
    result = import_tolls(
        session,
        _csv(
            _row("6", "NY LZA7293", "10/04/2026", "11:10:36 AM", "-9.11"),
            ',\' \',,,PAYMENT,,10/03/2026,,$25.00',
        ),
    )
    session.commit()
    assert result.rows == 1
    assert session.scalars(select(Toll)).one().amount_cents == 911


def test_the_ledger_totals_in_whole_cents(session, jerry):
    """Three tolls that are awkward in floating point. A figure somebody is
    going to be billed should not be the sum of 0.1 + 0.2."""
    import_tolls(
        session,
        _csv(
            _row("7", "NY LZA7293", "10/04/2026", "09:00:00 AM", "-0.10"),
            _row("8", "NY LZA7293", "10/04/2026", "10:00:00 AM", "-0.20"),
            _row("9", "NY LZA7293", "10/04/2026", "11:00:00 AM", "-6.94"),
        ),
    )
    session.commit()
    assert sum(t.amount_cents for t in session.scalars(select(Toll))) == 724


# ---------------------------------------------------------------------------
# Through the API
# ---------------------------------------------------------------------------


def test_uploading_a_statement_reports_what_it_did(session, jerry, api_client):
    _trip(session, jerry, guest="Jenna",
          starts=NOW - timedelta(days=1), ends=NOW + timedelta(days=1))
    session.commit()
    statement = _csv(
        _row("a1", "NY LZA7293", "10/04/2026", "11:10:36 AM", "-9.11"),
        _row("a2", " 00414500433", "10/04/2026", "11:13:32 AM", "-2.86", plaza="19"),
    )
    body = api_client.post(
        "/api/tolls/import",
        files={"statement": ("activity.csv", statement, "text/csv")},
    ).json()
    assert body["rows"] == 2
    assert body["matched"] == 1
    assert body["unmatched"] == 1
    assert body["unknown_tags"] == ["00414500433"], "so the operator knows what to bind"


def test_the_ledger_separates_what_is_owed_from_what_cannot_be_billed(
    session, jerry, api_client
):
    """Unattributed money is not revenue waiting to be collected, and a single
    total would read as though it were."""
    _trip(session, jerry, guest="Jenna",
          starts=NOW - timedelta(days=1), ends=NOW + timedelta(days=1))
    session.commit()
    api_client.post(
        "/api/tolls/import",
        files={
            "statement": (
                "a.csv",
                _csv(
                    _row("b1", "NY LZA7293", "10/04/2026", "11:10:36 AM", "-9.11"),
                    _row("b2", "NY ZZZ9999", "10/04/2026", "09:00:00 AM", "-4.00"),
                ),
                "text/csv",
            )
        },
    )
    body = api_client.get("/api/tolls").json()
    assert body["total_cents"] == 1311
    assert body["unrecovered_cents"] == 1311
    assert body["unattributed_cents"] == 400, "the stranger's crossing is not billable"
    assert [t["guest_name"] for t in body["tolls"] if t["guest_name"]] == ["Jenna"]


def test_marking_one_recovered_takes_it_out_of_what_is_owed(session, jerry, api_client):
    _trip(session, jerry, guest="Jenna",
          starts=NOW - timedelta(days=1), ends=NOW + timedelta(days=1))
    session.commit()
    api_client.post(
        "/api/tolls/import",
        files={"statement": (
            "a.csv", _csv(_row("c1", "NY LZA7293", "10/04/2026", "11:10:36 AM", "-9.11")), "text/csv"
        )},
    )
    toll_id = api_client.get("/api/tolls").json()["tolls"][0]["id"]

    api_client.post(f"/api/tolls/{toll_id}/recovered")
    after = api_client.get("/api/tolls").json()
    assert after["unrecovered_cents"] == 0
    assert after["total_cents"] == 911, "still on the statement, just no longer owed"

    api_client.post(f"/api/tolls/{toll_id}/recovered?undo=true")
    assert api_client.get("/api/tolls").json()["unrecovered_cents"] == 911


def test_a_file_that_is_not_a_statement_says_so(api_client):
    """The parser names the missing column and lists what it found, which is
    the difference between "fix your file" and "fix what"."""
    response = api_client.post(
        "/api/tolls/import",
        files={"statement": ("notes.csv", "a,b,c\n1,2,3\n", "text/csv")},
    )
    assert response.status_code == 422
    assert "missing required columns" in response.json()["detail"]


def test_rematch_after_binding_a_tag_reports_the_rescue(session, jerry, api_client):
    _trip(session, jerry, guest="Jenna",
          starts=NOW - timedelta(days=1), ends=NOW + timedelta(days=1))
    session.commit()
    api_client.post(
        "/api/tolls/import",
        files={"statement": (
            "a.csv",
            _csv(_row("d1", " 00414500433", "10/04/2026", "11:13:32 AM", "-2.86", plaza="19")),
            "text/csv",
        )},
    )
    assert api_client.get("/api/tolls").json()["unattributed_cents"] == 286

    jerry.ezpass_tag = "00414500433"
    session.commit()

    assert api_client.post("/api/tolls/rematch").json()["matched"] == 1
    assert api_client.get("/api/tolls").json()["unattributed_cents"] == 0


# ---------------------------------------------------------------------------
# Cents, and identity
# ---------------------------------------------------------------------------


def test_amounts_that_float_arithmetic_rounds_down(session, jerry):
    """$2.01 * 100 is 200.99999999999997 in binary floating point, so
    truncating loses a cent. 137 of the first 2000 possible toll amounts do
    this, and the first draft of these tests picked three that happened not to.
    A ledger that is a penny light per crossing is a ledger nobody reconciles.
    """
    import_tolls(
        session,
        _csv(
            _row("r1", "NY LZA7293", "10/04/2026", "09:00:00 AM", "-2.01"),
            _row("r2", "NY LZA7293", "10/04/2026", "10:00:00 AM", "-1.13"),
            _row("r3", "NY LZA7293", "10/04/2026", "11:00:00 AM", "-0.29"),
        ),
    )
    session.commit()
    amounts = sorted(t.amount_cents for t in session.scalars(select(Toll)))
    assert amounts == [29, 113, 201]


def test_two_identical_looking_crossings_are_kept_apart_by_their_ids(session, jerry):
    """Same plaza, same second, same amount, same tag — distinguishable only by
    EZPass's own transaction id. Falling back to hashing the crossing would
    treat the second as a duplicate and quietly drop it, which is the failure
    mode that makes a ledger disagree with the statement it came from.
    """
    both = _csv(
        _row("90001", " 00414500433", "10/04/2026", "11:13:32 AM", "-2.86", plaza="19"),
        _row("90002", " 00414500433", "10/04/2026", "11:13:32 AM", "-2.86", plaza="19"),
    )
    result = import_tolls(session, both)
    session.commit()
    assert result.imported == 2, "two charges, two rows"
    assert sum(t.amount_cents for t in session.scalars(select(Toll))) == 572
