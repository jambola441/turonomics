"""Additional distance: what a rental drove over its limit, and whether it was billed.

Turo works the overage out itself once the host enters the odometer at both
ends — `odometerDetail.excessDistance`, beside the trip's `distanceLimit` and
its `distanceOverageFee` per mile. Nothing here measures distance; it reads
Turo's figures and sets them against the distance lines Turo has invoiced, so
a trip that went over and was never billed for it stands out.

On the live account in October 2026, 8 of 134 finished trips went over and 4
of those were billed ($301.79); the other 4 ($93.33) never were.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

# The same words the ledger reads a distance line by.
_DISTANCE = re.compile(r"\b(?:distance|mileage|miles?|mi)\b", re.IGNORECASE)


@dataclass(frozen=True)
class Distance:
    included_mi: float | None
    driven_mi: float | None
    over_mi: float
    rate_cents: int | None
    # Over the limit at Turo's rate, to the cent.
    owed_cents: int
    # Distance lines on Turo's invoices for this rental.
    invoiced_cents: int
    # The tracker's miles over the rental (Bouncie's odometer), a check on
    # Turo's figure. Nothing is billed on it.
    tracker_mi: float | None = None

    @property
    def unbilled_cents(self) -> int:
        return max(self.owed_cents - self.invoiced_cents, 0)

    @property
    def tracker_over_mi(self) -> float | None:
        if self.tracker_mi is None or self.included_mi is None:
            return None
        return round(max(self.tracker_mi - self.included_mi, 0.0), 1)

    @property
    def disagrees(self) -> bool:
        """Whether Turo's miles and the tracker's are far enough apart to look
        at: both known, and apart by DISAGREE_MI and DISAGREE_SHARE of Turo's.

        Some gap is normal. The odometer readings on Turo are typed by the host
        from photos at hand-over; the tracker's span runs from the first drive
        in the rental to the last, and a delivery drive by the host can fall
        inside it.
        """
        if self.tracker_mi is None or self.driven_mi is None:
            return False
        gap = abs(self.tracker_mi - self.driven_mi)
        return gap >= DISAGREE_MI and gap >= DISAGREE_SHARE * max(self.driven_mi, 1.0)


DISAGREE_MI = 25.0
DISAGREE_SHARE = 0.10


def _scalar(value: Any) -> float | None:
    if not isinstance(value, Mapping) or value.get("unlimited") is True:
        return None
    number = value.get("scalar")
    if isinstance(number, bool) or not isinstance(number, (int, float)):
        return None
    return float(number)


def _rate_cents(fee: Any) -> int | None:
    """`{money: {amount}, distance: {scalar}}` as cents per mile."""
    if not isinstance(fee, Mapping):
        return None
    money = fee.get("money")
    amount = money.get("amount") if isinstance(money, Mapping) else None
    per = _scalar(fee.get("distance")) or 1.0
    if isinstance(amount, bool) or not isinstance(amount, (int, float)) or per <= 0:
        return None
    cents = Decimal(str(amount)) * 100 / Decimal(str(per))
    return int(cents.to_integral_value(ROUND_HALF_UP))


def distance_lines_cents(lines: Iterable[Any]) -> int:
    """The distance lines' total, from stored `[label, cents]` pairs."""
    total = 0
    for entry in lines:
        if (
            isinstance(entry, (list, tuple))
            and len(entry) == 2
            and isinstance(entry[0], str)
            and isinstance(entry[1], int)
            and _DISTANCE.search(entry[0])
        ):
            total += entry[1]
    return total


def _entered(odometer: Mapping[str, Any], driven: float | None) -> bool:
    """Whether the host entered the odometer on Turo for this trip.

    The operator sometimes skips it on a trip plainly under its limit, and
    Turo then reports 0 miles from readings that mean nothing — 5,313 then
    5,257 on David's, Jimmy's 29,929 against Jolene's 1,872 on Isabelle's.
    Taken as a figure, that read as a 500-mile disagreement with the tracker.
    Taken as what it is, the trip shows the tracker's miles alone.
    """
    if driven is None or driven <= 0:
        return False
    start = _scalar(odometer.get("checkInOdometerReading"))
    end = _scalar(odometer.get("checkOutOdometerReading"))
    return not (start is not None and end is not None and end < start)


def summarise(
    detail: Mapping[str, Any] | None,
    invoiced_cents: int = 0,
    tracker_mi: float | None = None,
) -> Distance | None:
    """Turo's distance figures for a rental, or None if it has none to give.

    None when neither Turo nor the tracker has a figure. With only the
    tracker's, Turo's side is empty and nothing is owed: what can be billed is
    what Turo worked out, and "0 over" from Turo would claim it had.
    """
    detail = detail if isinstance(detail, Mapping) else {}
    found = detail.get("odometerDetail")
    odometer: Mapping[str, Any] = found if isinstance(found, Mapping) else {}
    driven = _scalar(odometer.get("distanceDriven"))
    if not _entered(odometer, driven):
        driven = None
    booking = detail.get("booking")
    included = _scalar(odometer.get("distanceLimit"))
    if included is None and isinstance(booking, Mapping):
        included = _scalar(booking.get("distanceLimit"))
    if driven is None:
        if tracker_mi is None:
            return None
        return Distance(
            included_mi=included,
            driven_mi=None,
            over_mi=0.0,
            rate_cents=_rate_cents(detail.get("distanceOverageFee")),
            owed_cents=0,
            invoiced_cents=invoiced_cents,
            tracker_mi=tracker_mi,
        )
    over = _scalar(odometer.get("excessDistance")) or 0.0
    rate = _rate_cents(odometer.get("distanceOverageFee") or detail.get("distanceOverageFee"))
    owed = 0
    if over > 0 and rate is not None:
        owed = int((Decimal(str(over)) * rate).to_integral_value(ROUND_HALF_UP))
    return Distance(
        included_mi=included,
        driven_mi=driven,
        over_mi=over,
        rate_cents=rate,
        owed_cents=owed,
        invoiced_cents=invoiced_cents,
        tracker_mi=tracker_mi,
    )
