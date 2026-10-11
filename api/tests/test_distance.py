"""Additional distance, read off Turo's reservation detail.

The shape is the one stored for 61705384 on the live account (a 22-mile
overage at $0.35 a mile, invoiced); the other figures here are invented.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from datetime import timedelta as td
from typing import Any

from turonomics_api.db.models import (
    ReimbursementInvoice,
    Trip,
    TripSource,
    TripState,
    Vehicle,
)
from turonomics_api.ingest.distance import distance_lines_cents, summarise

from .conftest import requires_db


def _mi(n: float | None, unlimited: bool = False) -> dict[str, Any]:
    return {"unit": "MI", "scalar": n, "unlimited": unlimited}


def _detail(*, limit=200, driven=222, over=22, rate=0.35, per=1) -> dict[str, Any]:
    return {
        "booking": {"distanceLimit": _mi(limit)},
        "distanceOverageFee": {"money": {"amount": rate, "currencyCode": "USD"},
                               "distance": _mi(per)},
        "odometerDetail": {
            "distanceLimit": _mi(limit),
            "distanceDriven": _mi(driven),
            "excessDistance": _mi(over),
            "distanceOverageFee": {"money": {"amount": rate, "currencyCode": "USD"},
                                   "distance": _mi(per)},
            "checkInOdometerReading": _mi(46684),
            "checkOutOdometerReading": _mi(46906),
        },
    }


def test_an_overage_is_miles_over_at_turos_rate() -> None:
    found = summarise(_detail())
    assert found is not None
    assert (found.included_mi, found.driven_mi, found.over_mi) == (200, 222, 22)
    assert found.rate_cents == 35
    assert found.owed_cents == 770
    assert found.unbilled_cents == 770, "nothing invoiced yet"


def test_what_was_invoiced_for_distance_is_taken_off() -> None:
    assert summarise(_detail(), invoiced_cents=770).unbilled_cents == 0  # type: ignore[union-attr]
    assert summarise(_detail(), invoiced_cents=500).unbilled_cents == 270  # type: ignore[union-attr]
    assert summarise(_detail(), invoiced_cents=900).unbilled_cents == 0  # type: ignore[union-attr]


def test_a_rate_per_several_miles_is_per_mile() -> None:
    found = summarise(_detail(over=10, rate=2.0, per=4))
    assert found is not None and found.rate_cents == 50 and found.owed_cents == 500


def test_under_the_limit_owes_nothing() -> None:
    found = summarise(_detail(driven=150, over=0))
    assert found is not None and found.owed_cents == 0 and found.unbilled_cents == 0


def test_no_odometer_reading_is_no_answer_rather_than_zero() -> None:
    detail = _detail()
    detail["odometerDetail"]["distanceDriven"] = None
    assert summarise(detail) is None, "and no tracker either"
    assert summarise({}) is None
    assert summarise(None) is None


def test_unlimited_has_no_limit() -> None:
    detail = _detail(over=0)
    detail["odometerDetail"]["distanceLimit"] = _mi(None, unlimited=True)
    detail["booking"]["distanceLimit"] = _mi(None, unlimited=True)
    found = summarise(detail)
    assert found is not None and found.included_mi is None and found.owed_cents == 0


def test_the_trackers_miles_are_a_check_and_never_billed() -> None:
    close = summarise(_detail(), tracker_mi=230)
    assert close is not None and not close.disagrees and close.owed_cents == 770
    far = summarise(_detail(), tracker_mi=300)
    assert far is not None and far.disagrees
    assert far.tracker_over_mi == 100 and far.owed_cents == 770, "billing stays Turo's"


def test_a_small_trip_needs_a_real_gap_to_disagree() -> None:
    """10% of 40 miles is 4: an absolute floor stops every short trip flagging."""
    found = summarise(_detail(limit=200, driven=40, over=0), tracker_mi=52)
    assert found is not None and not found.disagrees
    found = summarise(_detail(limit=200, driven=40, over=0), tracker_mi=70)
    assert found is not None and found.disagrees


def test_a_long_trip_needs_a_real_share_to_disagree() -> None:
    """50 miles on a 1,000-mile trip is 5%: hand-typed odometers and a host's
    delivery drive account for that much."""
    found = summarise(_detail(limit=1200, driven=1000, over=0), tracker_mi=1050)
    assert found is not None and not found.disagrees
    found = summarise(_detail(limit=1200, driven=1000, over=0), tracker_mi=1150)
    assert found is not None and found.disagrees


def test_with_only_the_trackers_miles_nothing_is_owed() -> None:
    detail = _detail()
    detail["odometerDetail"]["distanceDriven"] = None
    found = summarise(detail, tracker_mi=260)
    assert found is not None
    assert found.driven_mi is None and found.owed_cents == 0 and found.tracker_over_mi == 60
    assert not found.disagrees, "nothing of Turo's to disagree with"


def test_an_odometer_not_entered_on_turo_is_not_a_figure() -> None:
    """David's: the host skipped the odometer, and Turo reported 0 miles from
    5,313 then 5,257. That is no reading, not a 540-mile disagreement."""
    detail = _detail(driven=0, over=0)
    detail["odometerDetail"]["checkInOdometerReading"] = _mi(5313)
    detail["odometerDetail"]["checkOutOdometerReading"] = _mi(5257)
    found = summarise(detail, tracker_mi=541)
    assert found is not None
    assert found.driven_mi is None and not found.disagrees and found.owed_cents == 0


def test_zero_miles_with_the_same_reading_twice_is_not_a_figure() -> None:
    detail = _detail(driven=0, over=0)
    detail["odometerDetail"]["checkInOdometerReading"] = _mi(5313)
    detail["odometerDetail"]["checkOutOdometerReading"] = _mi(5313)
    found = summarise(detail, tracker_mi=300)
    assert found is not None and found.driven_mi is None and not found.disagrees


def test_a_check_out_below_the_check_in_is_not_a_figure_either() -> None:
    detail = _detail(driven=120, over=0)
    detail["odometerDetail"]["checkInOdometerReading"] = _mi(29929)
    detail["odometerDetail"]["checkOutOdometerReading"] = _mi(1872)
    found = summarise(detail, tracker_mi=498)
    assert found is not None and found.driven_mi is None and not found.disagrees


def test_distance_lines_are_found_by_label() -> None:
    lines = [["22 mi additional distance", 770], ["Tolls", 3007], ["Additional mileage", 100],
             ["Cleaning", 4000], "junk", ["Tolls", "x"]]
    assert distance_lines_cents(lines) == 870


# ---------------------------------------------------------------------------
# On the trips list and the trip view
# ---------------------------------------------------------------------------

@requires_db
def test_the_list_and_view_say_what_distance_was_never_invoiced(
    api_client, monkeypatch, session
) -> None:
    monkeypatch.delenv("TOLLS_TOKEN", raising=False)
    car = Vehicle(nickname="Jimmy", make="Toyota", model="Corolla", year=2024, plate="LWH4685")
    session.add(car)
    session.flush()
    ends = datetime(2026, 9, 30, 18, 0, tzinfo=UTC)
    trip = Trip(
        vehicle_id=car.id, turo_trip_id="61705384", guest_name="Alexandria",
        starts_at=ends - td(days=2), ends_at=ends, state=TripState.completed,
        source=TripSource.email, turo_detail=_detail(over=22, rate=0.35),
    )
    session.add(trip)
    session.flush()
    session.add(ReimbursementInvoice(
        fingerprint=f"inv:{uuid.uuid4()}", reservation_id="61705384", state="charged",
        total_cents=500, lines=[["Tolls", 500]], toll_cents=500, trip_id=trip.id,
        first_seen_at=ends, last_seen_at=ends, charged_at=ends,
    ))
    session.commit()

    row = next(r for r in api_client.get("/api/trips?manual_only=false").json()["trips"]
               if r["id"] == str(trip.id))
    assert (row["distance_over_mi"], row["distance_unbilled_cents"]) == (22, 770)
    view = api_client.get(f"/api/trips/{trip.id}/view").json()
    assert view["distance"]["owed_cents"] == 770 and view["distance"]["invoiced_cents"] == 0

    session.add(ReimbursementInvoice(
        fingerprint=f"inv:{uuid.uuid4()}", reservation_id="61705384", state="charged",
        total_cents=770, lines=[["22 mi additional distance", 770]], toll_cents=None,
        trip_id=trip.id, first_seen_at=ends, last_seen_at=ends, charged_at=ends,
    ))
    session.commit()
    row = next(r for r in api_client.get("/api/trips?manual_only=false").json()["trips"]
               if r["id"] == str(trip.id))
    assert row["distance_unbilled_cents"] == 0, "invoiced now"


@requires_db
def test_the_ledger_carries_distance_beside_the_tolls(api_client, monkeypatch, session) -> None:
    """A rental with no crossings at all still has distance to file, and the
    total counts only rentals still inside the window."""
    monkeypatch.delenv("TOLLS_TOKEN", raising=False)
    car = Vehicle(nickname="Jerry", make="Toyota", model="Corolla", year=2024, plate="LZA7293")
    session.add(car)
    session.flush()
    now = datetime.now(UTC)
    for res, ended in (("70000001", now - td(days=10)), ("70000002", now - td(days=200))):
        session.add(Trip(
            vehicle_id=car.id, turo_trip_id=res, guest_name="Liam",
            starts_at=ended - td(days=3), ends_at=ended, state=TripState.completed,
            source=TripSource.email, turo_detail=_detail(over=124, rate=0.37),
        ))
    session.commit()

    body = api_client.get("/api/invoices/ledger").json()
    rows = {r["turo_trip_id"]: r for r in body["rows"]}
    assert rows["70000001"]["distance_unbilled_cents"] == 4588
    assert rows["70000002"]["distance_unbilled_cents"] == 4588
    assert body["distance_collectable_cents"] == 4588, "the closed one is not collectable"


@requires_db
def test_distance_past_the_window_is_counted_as_lost(api_client, monkeypatch, session) -> None:
    monkeypatch.delenv("TOLLS_TOKEN", raising=False)
    car = Vehicle(nickname="Jolene", make="Toyota", model="Corolla", year=2024, plate="LEH9892")
    session.add(car)
    session.flush()
    now = datetime.now(UTC)
    for res, ended, over in (("71000001", now - td(days=5), 10), ("71000002", now - td(days=300), 68)):
        session.add(Trip(
            vehicle_id=car.id, turo_trip_id=res, guest_name="Joe",
            starts_at=ended - td(days=3), ends_at=ended, state=TripState.completed,
            source=TripSource.email, turo_detail=_detail(over=over, rate=0.40),
        ))
    session.commit()
    body = api_client.get("/api/invoices/ledger").json()
    assert body["distance_collectable_cents"] == 400
    assert body["distance_lost_cents"] == 2720
