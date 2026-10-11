"""Tests for the tracker insets on a toll invoice's evidence sheet.

The sheet goes to a guest and to Turo. What these pin hardest is what it must
not do: draw any of the route beyond a short distance from a plaza, claim a
position at the minute of a charge (points carry no timestamps), or draw a
plaza that was a guess.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from datetime import timedelta as td
from xml.dom import minidom

import pytest

from turonomics_api.db.models import TelemetryEvent, Toll, Trip, TripSource, TripState, Vehicle
from turonomics_api.ingest import evidence_map
from turonomics_api.ingest.evidence import (
    INSET_RADIUS_M,
    EvidenceRow,
    EvidenceSheet,
    Inset,
    evidence_svg,
)
from turonomics_api.ingest.evidence_map import (
    MAX_INSETS,
    NEAR_M,
    build_insets,
    clip_segment,
    local_segments,
)
from turonomics_api.ingest.plazas import locate
from turonomics_api.ingest.trip_map import Drive, Placed, Route, _metres

from .conftest import requires_db

T0 = datetime(2026, 7, 10, 14, 0, tzinfo=UTC)
VNB = locate("VNB")
HCT = locate("HCT")
assert VNB is not None and HCT is not None
VNB_AT = (VNB.lat, VNB.lon)


def _through(lat: float, lon: float, span: float = 0.03) -> Route:
    """A track running east-west straight through a point."""
    drive = Drive(
        starts_at=T0, ends_at=T0 + td(minutes=30),
        points=[(lat, lon - span), (lat, lon), (lat, lon + span)],
    )
    return Route(drives=[drive], source="bouncie")


# ---------------------------------------------------------------------------
# Clipping
# ---------------------------------------------------------------------------
def test_a_leg_inside_the_circle_is_kept_whole() -> None:
    a, b = (VNB.lat, VNB.lon - 0.001), (VNB.lat, VNB.lon + 0.001)
    clipped = clip_segment(a, b, VNB_AT, INSET_RADIUS_M)
    assert clipped is not None
    assert clipped[0] == pytest.approx(a) and clipped[1] == pytest.approx(b)


def test_a_leg_through_the_circle_with_both_ends_outside_is_kept() -> None:
    """A sparse track jumps kilometres between fixes. Dropping a leg because
    neither end is inside would lose the one crossing that matters."""
    a, b = (VNB.lat, VNB.lon - 0.05), (VNB.lat, VNB.lon + 0.05)
    assert _metres(a, VNB_AT) > INSET_RADIUS_M and _metres(b, VNB_AT) > INSET_RADIUS_M
    clipped = clip_segment(a, b, VNB_AT, INSET_RADIUS_M)
    assert clipped is not None
    for end in clipped:
        assert _metres(end, VNB_AT) == pytest.approx(INSET_RADIUS_M, abs=5)


def test_a_leg_that_misses_the_circle_is_dropped() -> None:
    assert clip_segment((VNB.lat + 0.1, VNB.lon - 0.05), (VNB.lat + 0.1, VNB.lon + 0.05),
                        VNB_AT, INSET_RADIUS_M) is None


def test_a_leg_that_ends_before_reaching_the_circle_is_dropped() -> None:
    a, b = (VNB.lat, VNB.lon - 0.1), (VNB.lat, VNB.lon - 0.05)
    assert clip_segment(a, b, VNB_AT, INSET_RADIUS_M) is None


def test_no_point_beyond_the_radius_survives_clipping() -> None:
    """The privacy guarantee: a long route is cut to the circle before anything
    is drawn, so the rest of the trip is not in the data at all."""
    long_trip = Drive(
        starts_at=T0, ends_at=T0 + td(hours=3),
        points=[(VNB.lat + 0.2 * i / 20 - 0.1, VNB.lon + 0.01 * ((-1) ** i) * i / 10)
                for i in range(21)] + [VNB_AT],
    )
    segments = local_segments(Route(drives=[long_trip], source="bouncie"), VNB_AT, INSET_RADIUS_M)
    assert segments, "it did pass the plaza"
    for polyline in segments:
        for point in polyline:
            assert _metres(point, VNB_AT) <= INSET_RADIUS_M + 2


def test_a_route_that_never_comes_near_gives_nothing() -> None:
    far = _through(VNB.lat + 1.0, VNB.lon)
    assert local_segments(far, VNB_AT, INSET_RADIUS_M) == []


# ---------------------------------------------------------------------------
# Choosing the insets
# ---------------------------------------------------------------------------
def test_crossings_at_one_plaza_share_an_inset() -> None:
    later, earlier = T0 + td(hours=5), T0 + td(hours=1)
    insets, omitted = build_insets(
        _through(*VNB_AT), [("VNB", None, later), ("VNB", None, earlier)]
    )
    assert omitted == 0
    [inset] = insets
    assert inset.times == [earlier, later], "both times, in order"
    assert inset.closest_m < 50
    assert not inset.approximate
    assert inset.name and "Verrazzano" in inset.name


def test_a_plaza_with_an_approximate_point_says_so() -> None:
    [inset], _ = build_insets(_through(HCT.lat, HCT.lon), [("HCT", None, T0)])
    assert inset.approximate


def test_a_plaza_the_tracker_never_came_near_gets_no_inset() -> None:
    """Better a plain row than a picture that shows the car nowhere near."""
    route = _through(VNB.lat + 0.1, VNB.lon)  # about 11 km north
    assert build_insets(route, [("VNB", None, T0)]) == ([], 0)


def test_a_track_a_few_hundred_metres_off_still_counts_as_passing() -> None:
    """A sampled track can miss a gantry on a fast road by that much."""
    offset = 700 / 111_320  # about 700 m north
    [inset], _ = build_insets(_through(VNB.lat + offset, VNB.lon), [("VNB", None, T0)])
    assert 600 < inset.closest_m < NEAR_M


def test_a_track_between_the_threshold_and_the_circle_is_not_evidence() -> None:
    """About 1.1 km away: inside the drawn circle, so there is track to draw,
    but too far to say the car passed the plaza. The caption would be a stretch."""
    offset = 1100 / 111_320
    route = _through(VNB.lat + offset, VNB.lon)
    assert local_segments(route, VNB_AT, INSET_RADIUS_M), "there is track in the circle"
    assert build_insets(route, [("VNB", None, T0)]) == ([], 0)


def test_a_charging_zone_is_not_drawn() -> None:
    """A zone's point is its middle, and no gantry stands there."""
    zone = locate("CRZ")
    assert zone is not None and zone.zone
    assert build_insets(_through(zone.lat, zone.lon), [("CRZ", None, T0)]) == ([], 0)


def test_a_plaza_that_was_a_guess_is_not_drawn() -> None:
    low = locate("D95", "DELDOT")
    assert low is not None and low.confidence == "low", "this test needs a low-confidence row"
    assert build_insets(_through(low.lat, low.lon), [("D95", "DELDOT", T0)]) == ([], 0)


def test_a_plaza_nobody_can_place_is_not_drawn() -> None:
    assert build_insets(_through(*VNB_AT), [("NOSUCH", None, T0)]) == ([], 0)


def test_no_route_means_no_insets() -> None:
    assert build_insets(Route(), [("VNB", None, T0)]) == ([], 0)


def test_more_plazas_than_fit_are_counted_not_dropped_silently(monkeypatch) -> None:
    def fake_place(plaza: str, route: Route, agency: str | None = None) -> Placed:
        return Placed(lat=40.0 + 0.05 * int(plaza), lon=-74.0, how="plaza",
                      name=f"Plaza {plaza}", confidence="high")

    monkeypatch.setattr(evidence_map, "place", fake_place)
    track = Drive(starts_at=T0, ends_at=T0 + td(hours=2),
                  points=[(40.0 + 0.05 * i, -74.0) for i in range(-1, 14)])
    crossings = [(str(i), None, T0 + td(minutes=i)) for i in range(MAX_INSETS + 2)]
    insets, omitted = build_insets(Route(drives=[track], source="bouncie"), crossings)
    assert len(insets) == MAX_INSETS
    assert omitted == 2


# ---------------------------------------------------------------------------
# Drawing it
# ---------------------------------------------------------------------------
def _sheet(**extra) -> EvidenceSheet:
    return EvidenceSheet(
        reservation_id="53459553", guest_name="Avery", vehicle="Jolene", plate="LWH4685",
        starts_at=T0 - td(days=2), ends_at=T0,
        rows=[EvidenceRow(occurred_at=T0 - td(hours=5), plaza="VNB", amount_cents=1779)],
        **extra,
    )


def _inset(**over) -> Inset:
    base = dict(
        plaza="VNB", name="Verrazzano-Narrows Bridge", lat=VNB.lat, lon=VNB.lon,
        segments=[[(VNB.lat, VNB.lon - 0.01), (VNB.lat, VNB.lon), (VNB.lat, VNB.lon + 0.01)]],
        closest_m=12, times=[T0 - td(hours=5)],
    )
    base.update(over)
    return Inset(**base)  # type: ignore[arg-type]


def test_a_sheet_without_insets_is_exactly_what_it_was() -> None:
    plain = evidence_svg(_sheet())
    assert "tracker" not in plain
    assert plain == evidence_svg(_sheet(insets=(), track_source=None, insets_omitted=0))


def test_a_sheet_with_an_inset_is_valid_xml_and_says_what_it_shows() -> None:
    svg = evidence_svg(_sheet(insets=[_inset()], track_source="bouncie"))
    minidom.parseString(svg)  # a browser refuses broken XML and shows nothing
    assert "Where the car's tracker was near each plaza" in svg
    assert "tracker came within 12 m" in svg
    assert "500 m" in svg
    assert "not where the car was when it was charged" in svg
    assert "The rest of the trip is not shown" in svg
    assert "the car&apos;s GPS track" in svg or "the car's GPS track" in svg


def test_stored_positions_are_not_called_a_gps_track() -> None:
    svg = evidence_svg(_sheet(insets=[_inset()], track_source="telemetry"))
    assert "a fix every few minutes" in svg
    assert "GPS track" not in svg


def test_an_ampersand_in_a_plaza_name_does_not_break_the_image() -> None:
    svg = evidence_svg(_sheet(insets=[_inset(plaza="MTAB&T RKB", name="R&B <Bridge>")],
                              track_source="bouncie"))
    minidom.parseString(svg)
    assert "MTAB&amp;T RKB" in svg


def test_an_approximate_plaza_position_is_stated() -> None:
    svg = evidence_svg(_sheet(insets=[_inset(approximate=True)], track_source="bouncie"))
    assert "plaza position approximate" in svg


def test_a_long_plaza_name_is_cut_so_it_cannot_run_into_the_next_inset() -> None:
    """Seen on a rendered sheet: "Verrazzano-Narrows Bridge (Staten Island)"
    ran under the next column's caption and read as one sentence."""
    long_name = "Verrazzano-Narrows Bridge (Staten Island) upper level approach plaza"
    svg = evidence_svg(_sheet(insets=[_inset(name=long_name)], track_source="bouncie"))
    minidom.parseString(svg)
    assert long_name not in svg
    caption = re.search(r'font-weight="600" fill="#111827">(VNB[^<]*)</text>', svg)
    assert caption and caption.group(1).endswith("\u2026") and len(caption.group(1)) <= 34


def test_several_crossings_at_a_plaza_read_as_the_first_and_a_count() -> None:
    """The count is the part a cut would drop, so it must never be cut."""
    times = [T0 + td(hours=i) for i in range(6)]
    svg = evidence_svg(_sheet(insets=[_inset(times=times)], track_source="bouncie"))
    caption = re.search(r"(charged [^<]*)</text>", svg)
    assert caption
    assert caption.group(1) == "charged 10 Jul 10:00 AM +5 more"


def test_one_crossing_has_no_count() -> None:
    svg = evidence_svg(_sheet(insets=[_inset(times=[T0])], track_source="bouncie"))
    caption = re.search(r"(charged [^<]*)</text>", svg)
    assert caption and "more" not in caption.group(1)


def test_the_sheet_grows_to_hold_the_insets() -> None:
    def height(svg: str) -> int:
        match = re.search(r'<svg[^>]* height="(\d+)"', svg)
        assert match
        return int(match.group(1))

    plain = height(evidence_svg(_sheet()))
    one = height(evidence_svg(_sheet(insets=[_inset()], track_source="bouncie")))
    four = height(evidence_svg(_sheet(insets=[_inset()] * 4, track_source="bouncie")))
    assert one > plain
    assert four > one, "a fourth inset starts a second row"


# ---------------------------------------------------------------------------
# In the draft
# ---------------------------------------------------------------------------
@pytest.fixture()
def car(session):
    vehicle = Vehicle(nickname="Jolene", make="Toyota", model="Corolla", year=2024, plate="LWH4685")
    session.add(vehicle)
    session.flush()
    return vehicle


@pytest.fixture()
def rental(session, car):
    now = datetime.now(UTC)
    trip = Trip(
        vehicle_id=car.id, turo_trip_id="53459553", guest_name="Avery",
        starts_at=now - td(days=12), ends_at=now - td(days=10),
        state=TripState.completed, source=TripSource.email,
    )
    session.add(trip)
    session.flush()
    return trip


def _crossing(session, car, rental, plaza="VNB"):
    toll = Toll(
        vehicle_id=car.id, trip_id=rental.id, occurred_at=rental.ends_at - td(hours=30),
        plaza=plaza, amount_cents=1779, fingerprint=f"em-{uuid.uuid4()}",
    )
    session.add(toll)
    session.flush()
    return toll


def _telemetry(session, car, rental, lat, lon):
    for i, dlon in enumerate((-0.03, 0.0, 0.03)):
        session.add(TelemetryEvent(
            vehicle_id=car.id, event_type="poll",
            occurred_at=rental.ends_at - td(hours=32) + td(minutes=20 * i),
            location=f"SRID=4326;POINT({lon + dlon} {lat})", payload={},
        ))
    session.flush()


@requires_db
def test_a_draft_carries_the_inset_when_the_tracker_has_been_near(
    api_client, session, car, rental
) -> None:
    _crossing(session, car, rental)
    _telemetry(session, car, rental, VNB.lat, VNB.lon)
    session.commit()

    out = api_client.get("/api/invoices/next-draft")

    assert out.status_code == 200
    svg = out.json()["evidence_svg"]
    minidom.parseString(svg)
    assert "Where the car's tracker was near each plaza" in svg
    assert "a fix every few minutes" in svg, "stored positions, and it says so"


@requires_db
def test_a_draft_without_location_data_is_the_plain_table(
    api_client, session, car, rental
) -> None:
    _crossing(session, car, rental)
    session.commit()

    out = api_client.get("/api/invoices/next-draft")

    assert out.status_code == 200
    svg = out.json()["evidence_svg"]
    assert "tracker" not in svg
    assert "VNB" in svg, "and the crossing is still there"


@requires_db
def test_a_tracker_that_was_elsewhere_leaves_the_row_plain(
    api_client, session, car, rental
) -> None:
    _crossing(session, car, rental)
    _telemetry(session, car, rental, 42.65, -73.75)  # Albany
    session.commit()
    assert "tracker" not in api_client.get("/api/invoices/next-draft").json()["evidence_svg"]


@requires_db
def test_a_draft_never_fails_for_want_of_a_picture(
    api_client, session, car, rental, monkeypatch
) -> None:
    """Bouncie down, a bug in the drawing, anything: the invoice still drafts."""
    _crossing(session, car, rental)
    _telemetry(session, car, rental, VNB.lat, VNB.lon)
    session.commit()

    def boom(*args, **kwargs):
        raise RuntimeError("Bouncie is on fire")

    monkeypatch.setattr("turonomics_api.routers.invoices.trip_route", boom)
    out = api_client.get("/api/invoices/next-draft")

    assert out.status_code == 200
    assert "tracker" not in out.json()["evidence_svg"]
    assert out.json()["total_cents"] == 1779
