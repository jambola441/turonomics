"""A trip's receipt from Turo: what the guest paid and what reached the host.

The operator's probe of a trip page showed the call:

    GET /api/reservation/<id>/receipt
      earningLineItems:       [{label, value, receiptLineItemType}] — fees taken
                              off, and one typed TOTAL_EARNED
      reimbursementLineItems: [{label, value, receiptLineItemType:
                              CURRENCY_POSITIVE}] — incidentals the host was
                              reimbursed, tolls among them
      costDetailLineItems, depositLineItems, disputes, refundLineItems, ...

The probe masks text, so the labels were seen only by length. Nothing here
depends on their wording beyond `names_tolls`, the same test the email path
uses to find a toll line.

What it is for. Turo's invoice status says a claim was accepted, which has been
seen to come before the guest is charged, and nothing in the mail links every
charge back to its invoice. A reimbursement that appears on the host's receipt
for the exact amount of an invoice's toll line is the invoice having been
settled, whatever the status word was. That is the one inference made here, and
it is made only on an exact match for a rental whose invoice is not yet charged.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from turonomics_api.db.models import ReimbursementInvoice, Trip, TripState
from turonomics_api.gmail.parse import INVOICE_CHARGED, names_tolls
from turonomics_api.ingest.reimbursements import ReimbursementResult, _recover, mark_charged

# A receipt is re-read after this long while an invoice on the rental is still
# not charged, in case the money has landed since.
REREAD_AFTER = timedelta(days=1)

# How many trips with nothing outstanding are read per pull, so that every
# trip's receipt fills in over a few pulls without a burst of requests to Turo.
BACKFILL_PER_PULL = 10


def money_cents(value: Any) -> int | None:
    """"$30.07", "-$30.07", "(30.07)", 30.07 or {"amount": 30.07}, in cents."""
    if isinstance(value, Mapping):
        value = value.get("amount")
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        number = Decimal(str(value))
    elif isinstance(value, str):
        text = value.strip()
        negative = text.startswith("-") or (text.startswith("(") and text.endswith(")"))
        text = text.strip("()-").replace("$", "").replace(",", "").strip()
        try:
            number = Decimal(text)
        except InvalidOperation:
            return None
        if negative:
            number = -number
    else:
        return None
    return int((number * 100).to_integral_value(ROUND_HALF_UP))


def _lines(body: Mapping[str, Any], key: str) -> list[tuple[str, int, str]]:
    """(label, cents, type) for each readable line of one section."""
    items = body.get(key)
    out: list[tuple[str, int, str]] = []
    if not isinstance(items, list):
        return out
    for item in items:
        if not isinstance(item, Mapping):
            continue
        label = item.get("label")
        cents = money_cents(item.get("value"))
        kind = item.get("receiptLineItemType")
        if isinstance(label, str) and cents is not None:
            out.append((label.strip(), cents, kind if isinstance(kind, str) else ""))
    return out


def reimbursement_lines(body: Mapping[str, Any]) -> list[tuple[str, int]]:
    """What the host was reimbursed, line by line: positive amounts only."""
    return [(label, cents) for label, cents, _ in _lines(body, "reimbursementLineItems") if cents > 0]


def toll_lines(body: Mapping[str, Any]) -> list[int]:
    """The amounts of the receipt's reimbursement lines that are about tolls."""
    return [cents for label, cents in reimbursement_lines(body) if names_tolls(label)]


def total_earned_cents(body: Mapping[str, Any]) -> int | None:
    for _, cents, kind in _lines(body, "earningLineItems"):
        if kind == "TOTAL_EARNED":
            return cents
    return None


def is_receipt(body: Any) -> bool:
    """Whether a body is a receipt, as opposed to an error or an empty answer."""
    return isinstance(body, Mapping) and (
        isinstance(body.get("reimbursementLineItems"), list)
        or isinstance(body.get("earningLineItems"), list)
    )


def wanted_receipts(session: Session, *, now: datetime, limit: int = 20) -> list[str]:
    """Reservations whose receipt is worth reading this pull.

    First the rentals with a reimbursement that is not yet charged — the ones a
    receipt can settle — then, up to a small number, rentals never read, so the
    rest fill in over a few pulls.
    """
    outstanding = session.scalars(
        select(Trip.turo_trip_id)
        .join(ReimbursementInvoice, ReimbursementInvoice.reservation_id == Trip.turo_trip_id)
        .where(
            Trip.turo_trip_id.is_not(None),
            ReimbursementInvoice.state != INVOICE_CHARGED,
            or_(
                Trip.receipt_synced_at.is_(None),
                Trip.receipt_synced_at < now - REREAD_AFTER,
            ),
        )
        .order_by(Trip.ends_at.desc())
        .limit(limit)
    ).all()
    out = [r for r in dict.fromkeys(outstanding) if r]
    room = min(BACKFILL_PER_PULL, limit - len(out))
    if room > 0:
        fresh = session.scalars(
            select(Trip.turo_trip_id)
            .where(
                Trip.turo_trip_id.is_not(None),
                Trip.state == TripState.completed,
                Trip.receipt_synced_at.is_(None),
                Trip.ends_at < now,
            )
            .order_by(Trip.ends_at.desc())
            .limit(room + len(out))
        ).all()
        for r in fresh:
            if r and r not in out and room > 0:
                out.append(r)
                room -= 1
    return out


@dataclass
class ReceiptResult:
    stored: int = 0
    # Invoices made charged because the receipt shows their toll line paid.
    charged: int = 0
    tolls_recovered: int = 0
    unknown: list[str] = field(default_factory=list)
    # Bodies that were not receipts: a failed fetch is not "no receipt".
    unreadable: int = 0
    settled: list[str] = field(default_factory=list)


def settle_by_receipt(session: Session, trip: Trip, *, now: datetime, result: ReceiptResult) -> None:
    """Mark charged each uncharged invoice the receipt shows paid: by its toll
    line, and an invoice for anything else by every one of its lines."""
    _settle_tolls(session, trip, now=now, result=result)
    _settle_other_lines(session, trip, now=now, result=result)


# A receipt's own subtotal, "Reimbursement total", which repeats the lines.
_SUBTOTAL = re.compile(r"\btotal\b", re.IGNORECASE)


def _settle_other_lines(
    session: Session, trip: Trip, *, now: datetime, result: ReceiptResult
) -> None:
    """An invoice with no toll line, settled when each of its lines is on the
    receipt for exactly its amount.

    Ian Alexander's refueling: an invoice of $13.36 refueling and a $10.00
    convenience fee, ACCEPTED on Turo, and his receipt reading "Gas was not
    replaced $13.36" and "Convenience fee $10.00". Turo words the two
    differently, so amounts decide, not labels; and every line has to be
    there, so one coincidental amount settles nothing. Lines about tolls are
    left to the toll rule, and each receipt line pays one invoice line once.
    """
    body = trip.turo_receipt
    if not isinstance(body, Mapping) or trip.turo_trip_id is None:
        return
    pool = [
        cents
        for label, cents in reimbursement_lines(body)
        if not _SUBTOTAL.search(label) and not names_tolls(label)
    ]
    if not pool:
        return
    invoices = session.scalars(
        select(ReimbursementInvoice)
        .where(
            ReimbursementInvoice.reservation_id == trip.turo_trip_id,
            ReimbursementInvoice.toll_cents.is_(None),
        )
        .order_by(ReimbursementInvoice.first_seen_at)
    ).all()

    def amounts(invoice: ReimbursementInvoice) -> list[int]:
        return [
            entry[1]
            for entry in invoice.lines or []
            if isinstance(entry, list) and len(entry) == 2 and isinstance(entry[1], int)
        ]

    # What is already known charged takes its lines first, so they cannot be
    # read again as paying something else.
    for invoice in invoices:
        if invoice.state == INVOICE_CHARGED:
            for cents in amounts(invoice):
                if cents in pool:
                    pool.remove(cents)
    for invoice in invoices:
        if invoice.state == INVOICE_CHARGED:
            continue
        wanted = amounts(invoice)
        left = list(pool)
        found = bool(wanted)
        for cents in wanted:
            if cents not in left:
                found = False
                break
            left.remove(cents)
        if not found:
            continue
        pool = left
        if mark_charged(invoice, now=now):
            result.charged += 1
            result.settled.append(
                f"{trip.turo_trip_id}: ${invoice.total_cents / 100:,.2f} is on the receipt line by line"
            )
            if invoice.trip_id is None:
                invoice.trip_id = trip.id


def _settle_tolls(session: Session, trip: Trip, *, now: datetime, result: ReceiptResult) -> None:
    """Mark charged each uncharged invoice whose toll line the receipt shows paid.

    Exact amount, one receipt line per invoice. Anything looser — a total, a
    partial, a toll line on the receipt with no invoice to match — settles
    nothing, because the cost of being wrong is a crossing ticked off as paid
    that was not.
    """
    body = trip.turo_receipt
    if not isinstance(body, Mapping) or trip.turo_trip_id is None:
        return
    available = toll_lines(body)
    if not available:
        return
    invoices = session.scalars(
        select(ReimbursementInvoice).where(
            ReimbursementInvoice.reservation_id == trip.turo_trip_id,
            ReimbursementInvoice.state != INVOICE_CHARGED,
            ReimbursementInvoice.toll_cents.is_not(None),
        )
    ).all()
    for invoice in invoices:
        if invoice.toll_cents in available:
            available.remove(invoice.toll_cents)
            if mark_charged(invoice, now=now):
                result.charged += 1
                result.settled.append(
                    f"{trip.turo_trip_id}: ${(invoice.toll_cents or 0) / 100:,.2f} of tolls is on the receipt"
                )
                if invoice.trip_id is None:
                    invoice.trip_id = trip.id
                recovered = ReimbursementResult()
                _recover(session, invoice, recovered)
                result.tolls_recovered += recovered.tolls_recovered

    # One receipt line for several invoices: 44623088's receipt says $67.82 of
    # tolls, which is its $27.13 invoice and its $40.69 one together, and
    # neither matched alone. Exact again, and on the whole rental: every toll
    # line Turo invoiced on it, paid or not, against every toll line on the
    # receipt.
    left = [i for i in invoices if i.state != INVOICE_CHARGED]
    if not left:
        return
    every_line = session.scalars(
        select(ReimbursementInvoice.toll_cents).where(
            ReimbursementInvoice.reservation_id == trip.turo_trip_id,
            ReimbursementInvoice.toll_cents.is_not(None),
        )
    ).all()
    if sum(toll_lines(body)) != sum(c or 0 for c in every_line):
        return
    for invoice in left:
        if mark_charged(invoice, now=now):
            result.charged += 1
            result.settled.append(
                f"{trip.turo_trip_id}: ${(invoice.toll_cents or 0) / 100:,.2f} of tolls is "
                "within the receipt's toll total"
            )
            if invoice.trip_id is None:
                invoice.trip_id = trip.id
            recovered = ReimbursementResult()
            _recover(session, invoice, recovered)
            result.tolls_recovered += recovered.tolls_recovered


def resettle_receipts(session: Session, *, now: datetime) -> ReceiptResult:
    """Run the receipt check again over receipts already stored.

    For Re-match: a receipt is only read now and then, and a rule learned since
    should not wait for the next read to apply.
    """
    result = ReceiptResult()
    for trip in session.scalars(
        select(Trip)
        .join(ReimbursementInvoice, ReimbursementInvoice.reservation_id == Trip.turo_trip_id)
        .where(
            Trip.turo_receipt.is_not(None),
            ReimbursementInvoice.state != INVOICE_CHARGED,
        )
        .distinct()
    ):
        settle_by_receipt(session, trip, now=now, result=result)
    return result


def apply_receipt(
    session: Session, reservation_id: str, body: Any, *, now: datetime, result: ReceiptResult
) -> None:
    """Keep a receipt, and let it settle what it plainly settles."""
    trip = session.scalar(select(Trip).where(Trip.turo_trip_id == reservation_id))
    if trip is None:
        result.unknown.append(reservation_id)
        return
    if not is_receipt(body):
        result.unreadable += 1
        return
    trip.turo_receipt = dict(body)
    trip.receipt_synced_at = now
    result.stored += 1
    session.flush()
    settle_by_receipt(session, trip, now=now, result=result)


def receipt_summary(body: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """The receipt as a person reads it: lines with their amounts, and the total."""
    if not isinstance(body, Mapping):
        return None

    def show(cents: int) -> str:
        sign = "-" if cents < 0 else ""
        return f"{sign}${abs(cents) / 100:,.2f}"

    return {
        "earnings": [(label, show(cents)) for label, cents, _ in _lines(body, "earningLineItems")],
        "reimbursements": [(label, show(cents)) for label, cents, _ in _lines(body, "reimbursementLineItems")],
        "total_earned": show(c) if (c := total_earned_cents(body)) is not None else None,
    }
