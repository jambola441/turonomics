"""
Core matching logic: assigns EZPass toll charges to Turo trips.

Algorithm
---------
1. Build a transponder → set-of-plates lookup from the alias map
   (plate_to_transponder: dict[str, str]).

2. For each toll, collect candidate trips where:
       trip.start <= toll.timestamp <= trip.end
   AND
       the toll's plate matches the trip plate directly, OR
       the toll's transponder_id maps (via aliases) to the trip plate.

3. Tolls that match no trip go into `unmatched_tolls`.

Tie-breaking
------------
If a toll falls within overlapping trips (rare but possible with back-to-back
rentals of the same vehicle), it is assigned to the trip with the smallest
time window (most specific match).
"""

from turonomics_api.models import (
    EZPassToll,
    MatchResponse,
    TollEntry,
    TripTollResult,
    TuroTrip,
)

# plate → transponder_id  (keys and values are caller-normalized)
AliasMap = dict[str, str]


def match_tolls_to_trips(
    trips: list[TuroTrip],
    tolls: list[EZPassToll],
    aliases: AliasMap | None = None,
) -> MatchResponse:
    """Match EZPass tolls to Turo trips.

    Args:
        trips: Parsed Turo trips.
        tolls: Parsed EZPass toll transactions.
        aliases: Optional 1-to-1 map of license plate → transponder ID.
                 Enables matching transponder-only tolls to trips by plate.

    Returns:
        MatchResponse with per-trip toll breakdowns and any unmatched tolls.
    """
    # Build reverse lookup: transponder_id → set of plates
    transponder_to_plates: dict[str, set[str]] = {}
    if aliases:
        for plate, tid in aliases.items():
            transponder_to_plates.setdefault(tid, set()).add(plate)

    # Initialize result buckets
    trip_tolls: dict[str, list[TollEntry]] = {t.trip_id: [] for t in trips}
    unmatched: list[EZPassToll] = []

    for toll in tolls:
        # Plates this transponder is aliased to (empty set if unknown/no aliases)
        aliased_plates: set[str] = (
            transponder_to_plates.get(toll.transponder_id, set())
            if toll.transponder_id and aliases
            else set()
        )

        candidates: list[TuroTrip] = []
        for trip in trips:
            if not (trip.start <= toll.timestamp <= trip.end):
                continue

            # Direct plate match (toll carries a plate field)
            if toll.license_plate and toll.license_plate == trip.license_plate:
                candidates.append(trip)
            # Transponder → plate alias match
            elif trip.license_plate in aliased_plates:
                candidates.append(trip)

        if not candidates:
            unmatched.append(toll)
            continue

        # Assign to the most specific (shortest) trip window
        best = min(candidates, key=lambda t: (t.end - t.start))
        trip_tolls[best.trip_id].append(
            TollEntry(
                timestamp=toll.timestamp,
                plaza=toll.plaza,
                amount=toll.amount,
                transponder_id=toll.transponder_id,
                license_plate=toll.license_plate,
            )
        )

    results: list[TripTollResult] = [
        TripTollResult(
            trip_id=trip.trip_id,
            start=trip.start,
            end=trip.end,
            license_plate=trip.license_plate,
            tolls=trip_tolls[trip.trip_id],
            total_toll_amount=round(
                sum(t.amount for t in trip_tolls[trip.trip_id]), 2
            ),
        )
        for trip in trips
    ]

    return MatchResponse(trips=results, unmatched_tolls=unmatched)
