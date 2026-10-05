"""Attributing EZPass crossings to the guest who was driving.

Two failure modes here cost real money and both are quiet. Double-counting on
re-import inflates what you think you are owed; silently dropping a toll
nobody can be billed for shrinks it. The tests are mostly about those.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select

from turonomics_api.db.models import Toll, Trip, TripSource, TripState, Vehicle
from turonomics_api.ingest.tolls import import_tolls, rematch_unattributed

from .conftest import requires_db

pytestmark = requires_db

# Noon on the clock an EZPass statement prints, which is what the rows below
# say. Spelled in Eastern rather than UTC so the relationship between a trip
# window and a toll's printed time is visible: when this was UTC, "12:00:00 PM"
# in a row happened to line up only because the parser was handing back naive
# datetimes that Postgres then read as UTC.
EASTERN = ZoneInfo("America/New_York")
NOW = datetime(2026, 10, 4, 12, 0, tzinfo=EASTERN)

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
    statement = _csv(_row("3", " 99900000111", "10/04/2026", "11:13:32 AM", "-2.86", plaza="19"))

    loose = import_tolls(session, statement)
    session.commit()
    assert loose.unmatched == 1
    assert loose.unknown_tags == {"99900000111"}, "named, so it can be bound"

    jerry.ezpass_tag = "99900000111"
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
    statement = _csv(_row("4", " 99900000111", "10/04/2026", "11:13:32 AM", "-2.86", plaza="19"))
    import_tolls(session, statement)
    session.commit()

    jerry.ezpass_tag = "99900000111"
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
        _row("a2", " 99900000111", "10/04/2026", "11:13:32 AM", "-2.86", plaza="19"),
    )
    body = api_client.post(
        "/api/tolls/import",
        files={"statement": ("activity.csv", statement, "text/csv")},
    ).json()
    assert body["rows"] == 2
    assert body["matched"] == 1
    assert body["unmatched"] == 1
    assert body["unknown_tags"] == ["99900000111"], "so the operator knows what to bind"


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
            _csv(_row("d1", " 99900000111", "10/04/2026", "11:13:32 AM", "-2.86", plaza="19")),
            "text/csv",
        )},
    )
    assert api_client.get("/api/tolls").json()["unattributed_cents"] == 286

    jerry.ezpass_tag = "99900000111"
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
        _row("90001", " 99900000111", "10/04/2026", "11:13:32 AM", "-2.86", plaza="19"),
        _row("90002", " 99900000111", "10/04/2026", "11:13:32 AM", "-2.86", plaza="19"),
    )
    result = import_tolls(session, both)
    session.commit()
    assert result.imported == 2, "two charges, two rows"
    assert sum(t.amount_cents for t in session.scalars(select(Toll))) == 572


# ---------------------------------------------------------------------------
# The write endpoints
# ---------------------------------------------------------------------------
# These change a figure the operator bills a guest, and the API has no login.
# Open by default, closed by TOLLS_TOKEN — so the thing worth testing is that
# the variable actually closes them, and that a reader is not asked for a
# secret to look at the ledger.


def _statement() -> bytes:
    return _csv(_row("1", "NY LZA7293", "10/01/2026", "09:00:00 AM", "-9.11")).encode()


def test_importing_is_open_when_no_token_is_set(api_client, monkeypatch):
    monkeypatch.delenv("TOLLS_TOKEN", raising=False)
    response = api_client.post(
        "/api/tolls/import", files={"statement": ("activity.csv", _statement(), "text/csv")}
    )
    assert response.status_code == 200
    assert response.json()["imported"] == 1


def test_importing_without_the_token_is_refused_once_one_is_set(api_client, monkeypatch):
    monkeypatch.setenv("TOLLS_TOKEN", "letmein")
    response = api_client.post(
        "/api/tolls/import", files={"statement": ("activity.csv", _statement(), "text/csv")}
    )
    assert response.status_code == 401


def test_importing_with_the_token_is_allowed(api_client, monkeypatch, session):
    monkeypatch.setenv("TOLLS_TOKEN", "letmein")
    response = api_client.post(
        "/api/tolls/import",
        files={"statement": ("activity.csv", _statement(), "text/csv")},
        headers={"Authorization": "Bearer letmein"},
    )
    assert response.status_code == 200
    assert session.scalar(select(Toll).where(Toll.license_plate == "LZA7293")) is not None


def test_a_near_miss_token_is_refused(api_client, monkeypatch):
    monkeypatch.setenv("TOLLS_TOKEN", "letmein")
    response = api_client.post(
        "/api/tolls/import",
        files={"statement": ("activity.csv", _statement(), "text/csv")},
        headers={"Authorization": "Bearer letmei"},
    )
    assert response.status_code == 401


def test_rematching_and_ticking_are_behind_the_same_token(api_client, monkeypatch, session, jerry):
    monkeypatch.delenv("TOLLS_TOKEN", raising=False)
    api_client.post(
        "/api/tolls/import", files={"statement": ("activity.csv", _statement(), "text/csv")}
    )
    toll = session.scalar(select(Toll))
    assert toll is not None

    monkeypatch.setenv("TOLLS_TOKEN", "letmein")
    assert api_client.post("/api/tolls/rematch").status_code == 401
    assert api_client.post(f"/api/tolls/{toll.id}/recovered").status_code == 401

    auth = {"Authorization": "Bearer letmein"}
    assert api_client.post("/api/tolls/rematch", headers=auth).status_code == 200
    assert api_client.post(f"/api/tolls/{toll.id}/recovered", headers=auth).status_code == 200


def test_reading_the_ledger_never_needs_a_token(api_client, monkeypatch):
    """A token on the reads would mean the page cannot render at all.

    It also would not protect much: the rest of this API serves fleet positions
    and trip history unauthenticated. The token is about writes.
    """
    monkeypatch.setenv("TOLLS_TOKEN", "letmein")
    response = api_client.get("/api/tolls")
    assert response.status_code == 200
    assert response.json()["token_required"] is True


def test_the_ledger_says_when_no_token_is_needed(api_client, monkeypatch):
    monkeypatch.delenv("TOLLS_TOKEN", raising=False)
    assert api_client.get("/api/tolls").json()["token_required"] is False


# ---------------------------------------------------------------------------
# Two sources for one crossing
# ---------------------------------------------------------------------------
# The website renders "3:19 PM" and the download writes "03:19:07 PM" for the
# same toll. Before the fingerprint was cut to the minute, scraping the page and
# then uploading the official statement billed every crossing twice.


def _scraped(date_cell: str, amount: str = "-2.86", plaza: str = "RKB") -> bytes:
    """A row shaped like the account-activity page: one datetime, no txn id."""
    return (
        f"Tag/Plate #,Exit Plaza,Date,Amount\n"
        f'" 99900000111","{plaza}","{date_cell}","${amount}"\n'
    ).encode()


def test_the_same_crossing_from_both_sources_is_counted_once(session):
    from_page = import_tolls(session, _scraped("10/4/26 3:19 PM"))
    assert from_page.imported == 1

    # The download of the same crossing, with the seconds the page did not show.
    from_download = import_tolls(session, _scraped("10/04/2026 03:19:07 PM"))
    assert from_download.imported == 0, "the second source must not add a toll"
    assert from_download.already_known == 1
    assert session.scalar(select(func.count()).select_from(Toll)) == 1


def test_two_crossings_a_minute_apart_are_still_two(session):
    import_tolls(session, _scraped("10/4/26 3:19 PM"))
    import_tolls(session, _scraped("10/4/26 3:20 PM"))
    assert session.scalar(select(func.count()).select_from(Toll)) == 2


def test_two_tags_crossing_in_the_same_minute_do_not_collide(session):
    import_tolls(session, _scraped("10/4/26 3:19 PM"))
    other = (
        b"Tag/Plate #,Exit Plaza,Date,Amount\n"
        b'" 99900000222","RKB","10/4/26 3:19 PM","$-2.86"\n'
    )
    assert import_tolls(session, other).imported == 1
    assert session.scalar(select(func.count()).select_from(Toll)) == 2


def test_the_same_minute_at_different_plazas_is_two_crossings(session):
    import_tolls(session, _scraped("10/4/26 3:19 PM", plaza="RKB"))
    import_tolls(session, _scraped("10/4/26 3:19 PM", plaza="GWB"))
    assert session.scalar(select(func.count()).select_from(Toll)) == 2


def test_the_stored_time_keeps_its_seconds(session):
    """Only the hash is cut to the minute. The ledger still shows when, and the
    trip it fell inside is still decided on the real timestamp."""
    import_tolls(session, _scraped("10/04/2026 03:19:07 PM"))
    toll = session.scalar(select(Toll))
    assert toll is not None
    assert toll.occurred_at.second == 7


def test_a_transaction_id_still_wins_when_the_export_has_one(session):
    """Where both sources carry EZPass's own id, the id decides and the time is
    not consulted at all."""
    with_id = (
        b"Lane Txn ID,Tag/Plate #,Exit Plaza,Date,Amount\n"
        b'"33232151931"," 99900000111","RKB","10/4/26 3:19 PM","$-2.86"\n'
    )
    # Same id, a different time and amount — still the same crossing.
    restated = (
        b"Lane Txn ID,Tag/Plate #,Exit Plaza,Date,Amount\n"
        b'"33232151931"," 99900000111","RKB","10/4/26 9:00 PM","$-3.99"\n'
    )
    assert import_tolls(session, with_id).imported == 1
    assert import_tolls(session, restated).imported == 0
    assert session.scalar(select(func.count()).select_from(Toll)) == 1


# ---------------------------------------------------------------------------
# Undoing an import
# ---------------------------------------------------------------------------
# A ledger that can only grow is a ledger you cannot trust: a wrong file, or a
# page the scraper misread, inflates what you think you are owed with no way
# back. One fabricated row reached production before this existed.


def test_deleting_a_toll_removes_it_from_the_ledger(api_client, session, monkeypatch):
    monkeypatch.delenv("TOLLS_TOKEN", raising=False)
    api_client.post(
        "/api/tolls/import", files={"statement": ("a.csv", _statement(), "text/csv")}
    )
    toll = session.scalar(select(Toll))
    assert toll is not None

    response = api_client.delete(f"/api/tolls/{toll.id}")
    assert response.status_code == 200
    assert response.json()["deleted"] == 1
    assert session.scalar(select(func.count()).select_from(Toll)) == 0
    assert api_client.get("/api/tolls").json()["total_cents"] == 0


def test_deleting_the_same_toll_twice_is_not_an_error(api_client, session, monkeypatch):
    """A retried delete must not fail.

    A dropped connection on the first attempt is far more likely than a delete
    aimed at a row that never existed, and a 404 on the retry teaches the
    operator to distrust a delete that actually worked.
    """
    monkeypatch.delenv("TOLLS_TOKEN", raising=False)
    api_client.post(
        "/api/tolls/import", files={"statement": ("a.csv", _statement(), "text/csv")}
    )
    toll = session.scalar(select(Toll))
    assert toll is not None

    assert api_client.delete(f"/api/tolls/{toll.id}").json()["deleted"] == 1
    again = api_client.delete(f"/api/tolls/{toll.id}")
    assert again.status_code == 200
    assert again.json()["deleted"] == 0


def test_a_deleted_crossing_can_be_imported_again(api_client, session, monkeypatch):
    """Deleting releases the fingerprint.

    Otherwise a delete would be permanent in the worst way: the row gone and
    the statement unable to put it back.
    """
    monkeypatch.delenv("TOLLS_TOKEN", raising=False)
    api_client.post(
        "/api/tolls/import", files={"statement": ("a.csv", _statement(), "text/csv")}
    )
    toll = session.scalar(select(Toll))
    assert toll is not None
    api_client.delete(f"/api/tolls/{toll.id}")

    again = api_client.post(
        "/api/tolls/import", files={"statement": ("a.csv", _statement(), "text/csv")}
    )
    assert again.json()["imported"] == 1


def test_deleting_leaves_the_vehicle_and_the_trip_alone(api_client, session, monkeypatch, jerry):
    monkeypatch.delenv("TOLLS_TOKEN", raising=False)
    trip = Trip(
        vehicle_id=jerry.id,
        guest_name="Dana",
        starts_at=datetime(2026, 10, 1, 8, 0, tzinfo=UTC),
        ends_at=datetime(2026, 10, 1, 20, 0, tzinfo=UTC),
        state=TripState.completed,
        source=TripSource.email,
    )
    session.add(trip)
    session.flush()
    statement = _csv(_row("9", "NY LZA7293", "10/01/2026", "09:00:00 AM", "-9.11")).encode()
    api_client.post("/api/tolls/import", files={"statement": ("a.csv", statement, "text/csv")})

    toll = session.scalar(select(Toll))
    assert toll is not None and toll.trip_id == trip.id
    api_client.delete(f"/api/tolls/{toll.id}")

    session.expire_all()
    assert session.get(Trip, trip.id) is not None
    assert session.get(Vehicle, jerry.id) is not None


def test_deleting_needs_the_token(api_client, session, monkeypatch):
    monkeypatch.delenv("TOLLS_TOKEN", raising=False)
    api_client.post(
        "/api/tolls/import", files={"statement": ("a.csv", _statement(), "text/csv")}
    )
    toll = session.scalar(select(Toll))
    assert toll is not None

    monkeypatch.setenv("TOLLS_TOKEN", "letmein")
    assert api_client.delete(f"/api/tolls/{toll.id}").status_code == 401
    assert session.scalar(select(func.count()).select_from(Toll)) == 1

    ok = api_client.delete(
        f"/api/tolls/{toll.id}", headers={"Authorization": "Bearer letmein"}
    )
    assert ok.status_code == 200
    assert session.scalar(select(func.count()).select_from(Toll)) == 0


# ---------------------------------------------------------------------------
# The zone, where it costs money
# ---------------------------------------------------------------------------
# A four-hour error in a toll's timestamp is not a display problem. Attribution
# picks the trip whose window contains the crossing, so a shifted toll lands in
# the next guest's rental, or in nobody's. That is a wrong name on a bill.


def test_a_toll_is_billed_to_whoever_had_the_car_at_that_local_time(session, jerry):
    """An afternoon crossing during an afternoon rental.

    Shift the toll four hours and it leaves this window entirely, which is
    exactly what was happening: the crossing was real, the guest was real, and
    the charge went to neither.
    """
    _trip(session, jerry, guest="Dylan",
          starts=datetime(2026, 10, 4, 13, 0, tzinfo=EASTERN),
          ends=datetime(2026, 10, 4, 18, 0, tzinfo=EASTERN))
    import_tolls(session, _csv(_row("77", "NY LZA7293", "10/04/2026", "03:19:00 PM", "-6.94")))
    session.commit()
    assert session.scalars(select(Toll)).one().trip.guest_name == "Dylan"


def test_a_toll_outside_every_window_is_billed_to_nobody(session, jerry):
    """The other direction, so the test above cannot pass by attributing
    everything to the only trip there is."""
    _trip(session, jerry, guest="Dylan",
          starts=datetime(2026, 10, 4, 13, 0, tzinfo=EASTERN),
          ends=datetime(2026, 10, 4, 18, 0, tzinfo=EASTERN))
    import_tolls(session, _csv(_row("78", "NY LZA7293", "10/04/2026", "09:00:00 AM", "-6.94")))
    session.commit()
    toll = session.scalars(select(Toll)).one()
    assert toll.trip_id is None
    assert toll.vehicle.nickname == "Jerry"   # still known to be our car


def test_the_handover_hour_goes_to_the_right_guest(session, jerry):
    """Two rentals on one day, and a crossing in the second.

    With the timestamps four hours early this crossing fell inside the morning
    guest's window instead, which is the specific way the bug produced a
    plausible but wrong bill.
    """
    _trip(session, jerry, guest="Morning",
          starts=datetime(2026, 10, 4, 7, 0, tzinfo=EASTERN),
          ends=datetime(2026, 10, 4, 12, 0, tzinfo=EASTERN))
    _trip(session, jerry, guest="Afternoon",
          starts=datetime(2026, 10, 4, 13, 0, tzinfo=EASTERN),
          ends=datetime(2026, 10, 4, 19, 0, tzinfo=EASTERN))
    import_tolls(session, _csv(_row("79", "NY LZA7293", "10/04/2026", "02:30:00 PM", "-6.94")))
    session.commit()
    assert session.scalars(select(Toll)).one().trip.guest_name == "Afternoon"


def test_the_stored_time_reads_back_as_the_printed_time(session, jerry):
    """What the statement said, through the database, in fleet-local terms."""
    import_tolls(session, _csv(_row("80", "NY LZA7293", "10/04/2026", "03:19:00 PM", "-6.94")))
    session.commit()
    toll = session.scalars(select(Toll)).one()
    local = toll.occurred_at.astimezone(EASTERN)
    assert (local.hour, local.minute) == (15, 19)
    # And the stored instant is 7pm UTC, not 3pm — the shape of the original bug.
    assert toll.occurred_at.astimezone(UTC).hour == 19
