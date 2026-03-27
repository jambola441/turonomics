"""
Core matching logic: assigns EZPass toll charges to Turo trips.

Algorithm
---------
1. Build reverse-lookup dicts from the alias map:
       plate        -> owner name
       transponder  -> owner name

2. For each trip, resolve its owner from the plate (or leave as None if the
   plate is not in any alias).

3. For each toll, find trips where:
       trip.start <= toll.timestamp <= trip.end
   AND
       the toll's transponder or plate matches the trip owner
       (or matches the trip plate directly if no alias map is provided).

4. Tolls that match no trip go into `unmatched_tolls`.

Tie-breaking
------------
If a toll falls within overlapping trips (rare but possible with back-to-back
rentals of the same vehicle), it is assigned to the trip with the smallest
time window (most specific match).
"""

from turonomics_api.models import (
    EZPassToll,
    MatchResponse,
    OwnerAliases,
    TollEntry,
    TripTollResult,
    TuroTrip,
)


def _build_lookups(
    aliases: dict[str, OwnerAliases],
) -> tuple[dict[str, str], dict[str, str]]:
    """Return (plate_to_owner, transponder_to_owner) dicts."""
    plate_to_owner: dict[str, str] = {}
    transponder_to_owner: dict[str, str] = {}
    for owner, identity in aliases.items():
        for plate in identity.license_plates:
            plate_to_owner[plate] = owner
        for tid in identity.transponder_ids:
            transponder_to_owner[tid] = owner
    return plate_to_owner, transponder_to_owner


def match_tolls_to_trips(
    trips: list[TuroTrip],
    tolls: list[EZPassToll],
    aliases: dict[str, OwnerAliases] | None = None,
) -> MatchResponse:
    """Match EZPass tolls to Turo trips.

    Args:
        trips: Parsed Turo trips.
        tolls: Parsed EZPass toll transactions.
        aliases: Optional identity alias map. Keys are owner names; values
                 describe which plates and transponder IDs belong to that owner.

    Returns:
        MatchResponse with per-trip toll breakdowns and any unmatched tolls.
    """
    plate_to_owner: dict[str, str] = {}
    transponder_to_owner: dict[str, str] = {}
    if aliases:
        plate_to_owner, transponder_to_owner = _build_lookups(aliases)

    # Resolve owner for each trip
    trip_owners: dict[str, str | None] = {
        t.trip_id: plate_to_owner.get(t.license_plate) for t in trips
    }

    # Initialize result buckets
    trip_tolls: dict[str, list[TollEntry]] = {t.trip_id: [] for t in trips}
    unmatched: list[EZPassToll] = []

    for toll in tolls:
        # Determine what identity this toll belongs to
        toll_owner: str | None = (
            transponder_to_owner.get(toll.transponder_id)
            if toll.transponder_id
            else None
        )
        if toll_owner is None and toll.license_plate:
            toll_owner = plate_to_owner.get(toll.license_plate)

        # Find candidate trips: time window overlaps AND owner (or plate) matches
        candidates: list[TuroTrip] = []
        for trip in trips:
            if not (trip.start <= toll.timestamp <= trip.end):
                continue

            owner = trip_owners[trip.trip_id]

            if aliases:
                # Alias-aware match: both sides must resolve to the same owner
                if toll_owner is not None and owner == toll_owner:
                    candidates.append(trip)
                elif toll_owner is None and toll.license_plate == trip.license_plate:
                    # Toll has no owner in alias map; fall back to direct plate match
                    candidates.append(trip)
            else:
                # No alias map: match by plate only (toll plate or transponder not useful)
                if toll.license_plate and toll.license_plate == trip.license_plate:
                    candidates.append(trip)
                elif not toll.license_plate:
                    # No plate on toll and no alias map — cannot match
                    pass

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
            )
        )

    results: list[TripTollResult] = [
        TripTollResult(
            trip_id=trip.trip_id,
            start=trip.start,
            end=trip.end,
            license_plate=trip.license_plate,
            owner=trip_owners[trip.trip_id],
            tolls=trip_tolls[trip.trip_id],
            total_toll_amount=round(
                sum(t.amount for t in trip_tolls[trip.trip_id]), 2
            ),
        )
        for trip in trips
    ]

    return MatchResponse(trips=results, unmatched_tolls=unmatched)
