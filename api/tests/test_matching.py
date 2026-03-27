from datetime import datetime

from turonomics_api.matching import match_tolls_to_trips
from turonomics_api.models import EZPassToll, OwnerAliases, TuroTrip


def make_trip(
    trip_id: str,
    start: str,
    end: str,
    plate: str,
) -> TuroTrip:
    return TuroTrip(
        trip_id=trip_id,
        start=datetime.fromisoformat(start),
        end=datetime.fromisoformat(end),
        license_plate=plate,
    )


def make_toll(
    ts: str,
    transponder: str,
    amount: float,
    plaza: str = "Test Plaza",
    plate: str | None = None,
) -> EZPassToll:
    return EZPassToll(
        timestamp=datetime.fromisoformat(ts),
        transponder_id=transponder,
        amount=amount,
        plaza=plaza,
        license_plate=plate,
    )


ALIASES = {
    "John Smith": OwnerAliases(
        transponder_ids=["E-ZPass-123"],
        license_plates=["ABC1234", "XYZ5678"],
    ),
    "Jane Doe": OwnerAliases(
        transponder_ids=["E-ZPass-456"],
        license_plates=["LMN9999"],
    ),
}


class TestBasicMatching:
    def test_toll_within_trip_matched(self) -> None:
        trips = [make_trip("T1", "2024-06-01T08:00:00", "2024-06-03T20:00:00", "ABC1234")]
        tolls = [make_toll("2024-06-02T10:00:00", "E-ZPass-123", 19.0, plate="ABC1234")]
        result = match_tolls_to_trips(trips, tolls, ALIASES)
        assert len(result.trips[0].tolls) == 1
        assert result.trips[0].total_toll_amount == 19.0
        assert result.unmatched_tolls == []

    def test_toll_outside_trip_unmatched(self) -> None:
        trips = [make_trip("T1", "2024-06-01T08:00:00", "2024-06-03T20:00:00", "ABC1234")]
        tolls = [make_toll("2024-06-10T10:00:00", "E-ZPass-123", 19.0)]
        result = match_tolls_to_trips(trips, tolls, ALIASES)
        assert result.trips[0].tolls == []
        assert len(result.unmatched_tolls) == 1

    def test_multiple_tolls_single_trip(self) -> None:
        trips = [make_trip("T1", "2024-06-01T08:00:00", "2024-06-05T20:00:00", "ABC1234")]
        tolls = [
            make_toll("2024-06-01T14:00:00", "E-ZPass-123", 19.0),
            make_toll("2024-06-02T09:00:00", "E-ZPass-123", 16.0),
            make_toll("2024-06-03T11:00:00", "E-ZPass-123", 9.0),
        ]
        result = match_tolls_to_trips(trips, tolls, ALIASES)
        assert len(result.trips[0].tolls) == 3
        assert result.trips[0].total_toll_amount == 44.0

    def test_tolls_split_across_trips(self) -> None:
        trips = [
            make_trip("T1", "2024-06-01T08:00:00", "2024-06-03T20:00:00", "ABC1234"),
            make_trip("T2", "2024-06-05T08:00:00", "2024-06-07T20:00:00", "XYZ5678"),
        ]
        tolls = [
            make_toll("2024-06-02T10:00:00", "E-ZPass-123", 19.0),  # T1
            make_toll("2024-06-06T10:00:00", "E-ZPass-123", 16.0),  # T2
        ]
        result = match_tolls_to_trips(trips, tolls, ALIASES)
        t1 = next(t for t in result.trips if t.trip_id == "T1")
        t2 = next(t for t in result.trips if t.trip_id == "T2")
        assert len(t1.tolls) == 1
        assert len(t2.tolls) == 1


class TestAliasResolution:
    def test_transponder_matches_across_plates(self) -> None:
        """John's transponder should match tolls on either of his plates."""
        trips = [
            make_trip("T1", "2024-06-01T08:00:00", "2024-06-03T20:00:00", "ABC1234"),
            make_trip("T2", "2024-06-05T08:00:00", "2024-06-07T20:00:00", "XYZ5678"),
        ]
        tolls = [
            make_toll("2024-06-02T10:00:00", "E-ZPass-123", 19.0),
            make_toll("2024-06-06T10:00:00", "E-ZPass-123", 16.0),
        ]
        result = match_tolls_to_trips(trips, tolls, ALIASES)
        assert all(len(t.tolls) == 1 for t in result.trips)

    def test_owner_assigned_to_trip(self) -> None:
        trips = [make_trip("T1", "2024-06-01T08:00:00", "2024-06-03T20:00:00", "ABC1234")]
        tolls = [make_toll("2024-06-02T10:00:00", "E-ZPass-123", 19.0)]
        result = match_tolls_to_trips(trips, tolls, ALIASES)
        assert result.trips[0].owner == "John Smith"

    def test_different_owner_toll_not_matched(self) -> None:
        """Jane's transponder should not match a trip belonging to John's plate."""
        trips = [make_trip("T1", "2024-06-01T08:00:00", "2024-06-03T20:00:00", "ABC1234")]
        tolls = [make_toll("2024-06-02T10:00:00", "E-ZPass-456", 19.0)]  # Jane's
        result = match_tolls_to_trips(trips, tolls, ALIASES)
        assert result.trips[0].tolls == []
        assert len(result.unmatched_tolls) == 1

    def test_unknown_transponder_plate_fallback(self) -> None:
        """A toll with a transponder not in any alias falls back to plate match."""
        trips = [make_trip("T1", "2024-06-01T08:00:00", "2024-06-03T20:00:00", "ABC1234")]
        tolls = [
            make_toll("2024-06-02T10:00:00", "UNKNOWN-TAG", 19.0, plate="ABC1234")
        ]
        result = match_tolls_to_trips(trips, tolls, ALIASES)
        assert len(result.trips[0].tolls) == 1

    def test_no_alias_map_plate_only_match(self) -> None:
        trips = [make_trip("T1", "2024-06-01T08:00:00", "2024-06-03T20:00:00", "ABC1234")]
        tolls = [make_toll("2024-06-02T10:00:00", "ANY-TAG", 19.0, plate="ABC1234")]
        result = match_tolls_to_trips(trips, tolls, aliases=None)
        assert len(result.trips[0].tolls) == 1


class TestEdgeCases:
    def test_toll_at_trip_boundary_start(self) -> None:
        trips = [make_trip("T1", "2024-06-01T10:00:00", "2024-06-03T18:00:00", "ABC1234")]
        tolls = [make_toll("2024-06-01T10:00:00", "E-ZPass-123", 5.0)]
        result = match_tolls_to_trips(trips, tolls, ALIASES)
        assert len(result.trips[0].tolls) == 1

    def test_toll_at_trip_boundary_end(self) -> None:
        trips = [make_trip("T1", "2024-06-01T10:00:00", "2024-06-03T18:00:00", "ABC1234")]
        tolls = [make_toll("2024-06-03T18:00:00", "E-ZPass-123", 5.0)]
        result = match_tolls_to_trips(trips, tolls, ALIASES)
        assert len(result.trips[0].tolls) == 1

    def test_overlapping_trips_shortest_wins(self) -> None:
        """Toll goes to the most specific (shortest) overlapping trip."""
        trips = [
            make_trip("T-LONG", "2024-06-01T00:00:00", "2024-06-10T00:00:00", "ABC1234"),
            make_trip("T-SHORT", "2024-06-02T08:00:00", "2024-06-03T20:00:00", "ABC1234"),
        ]
        tolls = [make_toll("2024-06-02T12:00:00", "E-ZPass-123", 10.0)]
        result = match_tolls_to_trips(trips, tolls, ALIASES)
        short = next(t for t in result.trips if t.trip_id == "T-SHORT")
        long_ = next(t for t in result.trips if t.trip_id == "T-LONG")
        assert len(short.tolls) == 1
        assert len(long_.tolls) == 0

    def test_empty_trips_all_unmatched(self) -> None:
        tolls = [make_toll("2024-06-02T10:00:00", "E-ZPass-123", 19.0)]
        result = match_tolls_to_trips([], tolls, ALIASES)
        assert result.trips == []
        assert len(result.unmatched_tolls) == 1

    def test_empty_tolls_zero_amounts(self) -> None:
        trips = [make_trip("T1", "2024-06-01T08:00:00", "2024-06-03T20:00:00", "ABC1234")]
        result = match_tolls_to_trips(trips, [], ALIASES)
        assert result.trips[0].tolls == []
        assert result.trips[0].total_toll_amount == 0.0

    def test_total_amount_rounded(self) -> None:
        trips = [make_trip("T1", "2024-06-01T08:00:00", "2024-06-05T20:00:00", "ABC1234")]
        tolls = [
            make_toll("2024-06-01T10:00:00", "E-ZPass-123", 0.1),
            make_toll("2024-06-02T10:00:00", "E-ZPass-123", 0.2),
        ]
        result = match_tolls_to_trips(trips, tolls, ALIASES)
        assert result.trips[0].total_toll_amount == 0.3  # not 0.30000000000000004
