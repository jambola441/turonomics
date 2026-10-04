"""Where to move a car to, not just that you must.

The app already knows a deadline is coming and shouts about it. The operator's
actual problem at 8am is the next question: *where*. Driving around looking for
a block that is not swept tomorrow is the expensive part.

Everything needed is already loaded — 886 segment sides around 11238, each with
its cleaning rules — so this ranks the nearby ones by how much time parking
there would buy.

**What this does not know is whether there is a space.** It knows the rules, not
the kerb. A suggestion means "this block is not swept until Thursday", never
"there is room here". Blurring those two would make the app confidently wrong
about the one thing it cannot see, so the distinction is in the field names and
has to stay in the UI.

Sides with no rules on file are left out rather than ranked first. No rules
means nobody has read a sign there, not that the street is never swept — and
"park here, we know nothing about it" is advice worth less than silence.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from turonomics_api.asp.schedule import CleaningWindow, next_cleaning_window
from turonomics_api.db.models import AspRule, StreetSegmentSide, Vehicle
from turonomics_api.ingest.tasks import rules_for, suspended_dates
from turonomics_api.settings import fleet_timezone

# How far the operator will walk back from a spot. Five minutes or so in
# Brooklyn; further and they would drive, at which point the whole
# neighbourhood is in range and the list stops being a decision.
DEFAULT_RADIUS_M = 400.0

# Enough to choose between, few enough to read on a phone while double parked.
DEFAULT_LIMIT = 5

# A block swept in the next couple of hours is not somewhere to move *to*.
MINIMUM_RESPITE = timedelta(hours=3)


@dataclass(frozen=True)
class Spot:
    """A block with a known schedule, and what parking there would buy."""

    segment_side_id: uuid.UUID
    street_name: str
    side: str
    from_cross_street: str | None
    to_cross_street: str | None
    distance_m: float
    # None means no cleaning is scheduled within the horizon the rules cover.
    next_cleaning: datetime | None
    fits_van: bool | None

    def respite(self, *, now: datetime) -> timedelta | None:
        if self.next_cleaning is None:
            return None
        return self.next_cleaning - now


def _window(session: Session, side_id: uuid.UUID, *, now: datetime) -> CleaningWindow | None:
    return next_cleaning_window(
        rules_for(session, side_id),
        now=now,
        suspended_dates=suspended_dates(session),
        tz=fleet_timezone(),
    )


def suggest_spots(
    session: Session,
    *,
    vehicle: Vehicle,
    lat: float,
    lon: float,
    now: datetime | None = None,
    radius_m: float = DEFAULT_RADIUS_M,
    limit: int = DEFAULT_LIMIT,
    exclude_side_id: uuid.UUID | None = None,
) -> list[Spot]:
    """Nearby blocks worth moving to, the longest respite first.

    Ranked by how long until that block is next swept rather than by distance,
    because the point is to stop thinking about this car for a while. Distance
    is reported so the operator can overrule that — a quiet block four hundred
    metres away is not obviously better than a decent one across the street,
    and this does not pretend to know which.
    """
    now = now or datetime.now(UTC)
    point = func.ST_GeogFromText(f"SRID=4326;POINT({lon} {lat})")
    distance = func.ST_Distance(StreetSegmentSide.geom, point)

    query = (
        select(StreetSegmentSide, distance.label("distance_m"))
        .where(StreetSegmentSide.geom.is_not(None))
        .where(func.ST_DWithin(StreetSegmentSide.geom, point, radius_m))
        # Only sides somebody has actually read a sign for. A side with no
        # rules would otherwise sort to the top as "never swept", which is a
        # confident answer built out of missing data.
        .where(select(AspRule.id).where(AspRule.segment_side_id == StreetSegmentSide.id).exists())
        .order_by(distance)
    )
    if vehicle.needs_large_spot:
        query = query.where(StreetSegmentSide.fits_van.is_not(False))
    if exclude_side_id is not None:
        query = query.where(StreetSegmentSide.id != exclude_side_id)

    found: list[Spot] = []
    for side, distance_m in session.execute(query).all():
        window = _window(session, side.id, now=now)
        if window is not None and window.starts_at - now < MINIMUM_RESPITE:
            # Swept again before the operator could reasonably get back to it.
            continue
        found.append(
            Spot(
                segment_side_id=side.id,
                street_name=side.street_name,
                side=str(side.side.value),
                from_cross_street=side.from_cross_street,
                to_cross_street=side.to_cross_street,
                distance_m=float(distance_m),
                next_cleaning=window.starts_at if window else None,
                fits_van=side.fits_van,
            )
        )

    def rank(spot: Spot) -> tuple[int, float, float]:
        # Nothing scheduled first, then the latest sweep, then the nearest.
        # Negating the timestamp puts a later date earlier in the ordering,
        # which is the whole point: the best spot is the one you can forget
        # about for longest.
        if spot.next_cleaning is None:
            return (0, 0.0, spot.distance_m)
        return (1, -spot.next_cleaning.timestamp(), spot.distance_m)

    found.sort(key=rank)
    return found[:limit]
