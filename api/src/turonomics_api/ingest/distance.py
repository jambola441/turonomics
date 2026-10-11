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

    @property
    def unbilled_cents(self) -> int:
        return max(self.owed_cents - self.invoiced_cents, 0)


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


def summarise(detail: Mapping[str, Any] | None, invoiced_cents: int = 0) -> Distance | None:
    """Turo's distance figures for a rental, or None if it has none to give.

    None for a rental with no odometer reading at either end: Turo has not
    worked anything out, and "0 over" would claim it had.
    """
    if not isinstance(detail, Mapping):
        return None
    odometer = detail.get("odometerDetail")
    if not isinstance(odometer, Mapping):
        return None
    driven = _scalar(odometer.get("distanceDriven"))
    if driven is None:
        return None
    booking = detail.get("booking")
    included = _scalar(odometer.get("distanceLimit"))
    if included is None and isinstance(booking, Mapping):
        included = _scalar(booking.get("distanceLimit"))
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
    )
