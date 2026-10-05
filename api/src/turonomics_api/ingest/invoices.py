"""What to bill each guest, and how long is left to do it.

The ledger answers "which crossings has this account been charged for". This
answers the next question, which is the one that gets money back: for each
rental, what does that guest owe, and when does the chance to ask expire.

Turo gives ninety days from the end of a trip to file a toll reimbursement.
That is the only deadline here that loses money by passing quietly — an
unbilled toll inside the window is a reminder, and the same toll outside it is
simply gone. So the window is computed, not left to be remembered, and an
invoice past it is reported as past it rather than mixed in with the work.

One invoice per rental, not per crossing: a guest with twenty crossings gets
one bill with twenty lines, which is also how Turo's own form expects it.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from turonomics_api.db.models import Toll, Trip, TripSource
from turonomics_api.settings import toll_filing_window_days

# A rental recorded by hand was arranged off-platform, so there is no Turo
# reimbursement to file and no Turo clock. It still gets an invoice — the guest
# still owes it — just one that has to be sent some other way.
OFF_PLATFORM = TripSource.manual


@dataclass(frozen=True)
class InvoiceLine:
    toll_id: uuid.UUID
    occurred_at: datetime
    plaza: str
    amount_cents: int
    # Set when this crossing is on the invoice because the car came back late.
    # Worth showing: it is the line a guest is most likely to query.
    overrun_seconds: int | None = None


@dataclass
class Invoice:
    trip_id: uuid.UUID
    guest_name: str | None
    vehicle_nickname: str | None
    starts_at: datetime
    ends_at: datetime
    off_platform: bool
    # Turo's own id for the rental, so the page can link straight to the place
    # the invoice would be filed.
    turo_trip_id: str | None
    lines: list[InvoiceLine] = field(default_factory=list)

    @property
    def total_cents(self) -> int:
        return sum(line.amount_cents for line in self.lines)

    def file_by(self, window_days: int) -> datetime | None:
        """The last day Turo will accept this, or None off-platform."""
        if self.off_platform:
            return None
        return self.ends_at + timedelta(days=window_days)

    def days_left(self, window_days: int, now: datetime) -> int | None:
        """Whole days remaining. Negative once the window has closed.

        Rounded down towards the deadline rather than away from it: on the last
        day this reads 0, not 1, because a day that is nearly gone is not a day
        in hand.
        """
        deadline = self.file_by(window_days)
        if deadline is None:
            return None
        return (deadline - now) // timedelta(days=1)


def build_invoices(
    session: Session, *, now: datetime, include_recovered: bool = False
) -> list[Invoice]:
    """Every rental with something still to bill, soonest deadline first.

    Only crossings that are attributed and not yet recovered: a crossing nobody
    owes is not an invoice, and one already billed back is not either.
    Off-platform rentals sort last, since nothing about them expires.
    """
    # The join is what excludes a crossing nobody owes — it is inner, so a null
    # trip_id has nothing to join to. A `where trip_id is not null` beside it
    # read as the thing doing that work and was doing none; mutating it away
    # changed no test. What holds the line is the test asserting an
    # unattributed crossing produces no invoice.
    query = (
        select(Toll)
        .join(Trip, Toll.trip_id == Trip.id)
        .order_by(Toll.occurred_at.asc())
    )
    if not include_recovered:
        query = query.where(Toll.recovered_at.is_(None))

    invoices: dict[uuid.UUID, Invoice] = {}
    for toll in session.scalars(query):
        trip = toll.trip
        if trip is None:
            # Unreachable: the join is inner. Not defensive padding either —
            # Toll.trip is `Trip | None`, so --strict requires the check before
            # trip.ends_at is touched. It is why making the join outer survives
            # mutation, which is the right outcome rather than a gap: the guard
            # is the type's, and the join is the filter.
            continue  # pragma: no cover
        invoice = invoices.get(trip.id)
        if invoice is None:
            invoice = Invoice(
                trip_id=trip.id,
                guest_name=trip.guest_name,
                vehicle_nickname=trip.vehicle.nickname if trip.vehicle else None,
                starts_at=trip.starts_at,
                ends_at=trip.ends_at,
                off_platform=trip.source is OFF_PLATFORM,
                turo_trip_id=trip.turo_trip_id,
            )
            invoices[trip.id] = invoice
        overrun = (
            int((toll.occurred_at - trip.ends_at).total_seconds())
            if toll.occurred_at > trip.ends_at
            else None
        )
        invoice.lines.append(
            InvoiceLine(
                toll_id=toll.id,
                occurred_at=toll.occurred_at,
                plaza=toll.plaza,
                amount_cents=toll.amount_cents,
                overrun_seconds=overrun,
            )
        )

    window = toll_filing_window_days()

    def urgency(invoice: Invoice) -> tuple[int, float]:
        left = invoice.days_left(window, now)
        # Off-platform last: nothing about it expires, so it should never push
        # a Turo invoice with six days left down the page.
        return (1, 0.0) if left is None else (0, float(left))

    return sorted(invoices.values(), key=urgency)
