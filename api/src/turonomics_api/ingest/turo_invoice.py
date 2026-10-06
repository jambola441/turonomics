"""What a reimbursement invoice charged for, from Turo's invoice page.

The notification emails are the only other record of a reimbursement, and they
are uneven about it. The "filed" email itemises; the "has been charged" email
links the receipt instead and often gives a total and nothing else. An invoice
seen only through that email is a number nobody here can break down — Austin's
$140.40 on 59077848 is one — and a rental carrying one cannot be filed for,
because the number may already include the tolls.

Turo's invoice page can break it down, and states it as an enum rather than a
label to be read with a regular expression:

    GET /api/v2/reservations/<id>/reimbursement/invoice/<invoiceId>
    lineItems: [{type: TOLL_REIMBURSEMENT, title, total: {amount}}]

The extension fetches that with the session the browser holds and posts the
body here unmodified. This reads it, finds the invoice it belongs to, and
replaces what the email said about its lines with what Turo says.

Only ``TOLL`` types count as tolls. Every other type is a charge that is not
tolls, whatever it is called — the point of the enum is that it does not need
reading, and the other values (tickets, additional distance, refuelling) have
not all been observed, so a list of them would be a guess. An unrecognised type
is therefore "not tolls" rather than "unreadable", which is the right side to
err on only because `TOLL_REIMBURSEMENT` itself has been observed.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from turonomics_api.db.models import ReimbursementInvoice, Toll, Trip
from turonomics_api.gmail.parse import INVOICE_FILED, names_tolls
from turonomics_api.ingest.reimbursements import ReimbursementResult, _recover

log = logging.getLogger("turonomics.ingest.turo_invoice")


@dataclass(frozen=True)
class TuroLine:
    type: str
    title: str
    cents: int

    @property
    def is_tolls(self) -> bool:
        # A TOLL token in the enum, not a substring: TOLL_REIMBURSEMENT is the
        # observed value, and a hypothetical PAYTOLL_FEE is not tolls.
        return "TOLL" in self.type.upper().split("_")


@dataclass(frozen=True)
class TuroInvoice:
    reservation_id: str
    invoice_id: str
    reimbursement_id: str | None
    status: str | None
    total_cents: int
    total_before_fees_cents: int | None
    lines: tuple[TuroLine, ...]

    @property
    def toll_cents(self) -> int | None:
        """The toll lines' total, or None when the invoice charged no tolls."""
        tolls = [line.cents for line in self.lines if line.is_tolls]
        return sum(tolls) if tolls else None


def _cents(money: Any) -> int | None:
    """``{amount, currencyCode}`` in dollars, to integer cents.

    Through Decimal from the string form, so 140.4 is 14040 and not 14039 — a
    float multiplied by a hundred is the classic way to lose a cent.
    """
    if not isinstance(money, Mapping):
        return None
    amount = money.get("amount")
    if isinstance(amount, bool) or not isinstance(amount, (int, float, str)):
        return None
    try:
        return int((Decimal(str(amount)) * 100).to_integral_value())
    except InvalidOperation:
        return None


def _id(value: Any) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    text = str(value).strip()
    return text or None


def parse_turo_invoice(reservation_id: str, body: Mapping[str, Any]) -> TuroInvoice | None:
    """One invoice-page body, or None if it is not one.

    None rather than raising, as with reservation detail: the extension posts
    what it got, and a Turo error body is a thing to count, not a crash.
    """
    invoice_id = _id(body.get("invoiceId"))
    total = _cents(body.get("total"))
    items = body.get("lineItems")
    if invoice_id is None or total is None or not isinstance(items, list):
        return None
    lines: list[TuroLine] = []
    for item in items:
        if not isinstance(item, Mapping):
            return None
        kind = item.get("type")
        cents = _cents(item.get("total"))
        if not isinstance(kind, str) or cents is None:
            # One line unreadable makes the whole invoice unreadable. Keeping
            # the rest would read as "these are all its charges" when they
            # are not, which is the exact question this exists to answer.
            return None
        title = item.get("title")
        lines.append(TuroLine(kind, title.strip() if isinstance(title, str) else kind, cents))
    status = body.get("reimbursementStatus")
    return TuroInvoice(
        reservation_id=reservation_id,
        invoice_id=invoice_id,
        reimbursement_id=_id(body.get("reimbursementId")),
        status=status if isinstance(status, str) else None,
        total_cents=total,
        total_before_fees_cents=_cents(body.get("totalBeforeFees")),
        lines=tuple(lines),
    )


@dataclass
class TuroInvoiceResult:
    seen: int = 0
    # Matched to an invoice the mail had already recorded.
    matched: int = 0
    # Not known from mail at all, so recorded from this.
    created: int = 0
    # Invoices whose toll share is now known where it was not before.
    newly_itemised: list[str] = field(default_factory=list)
    # Crossings stamped as asked for, or ticked off as paid, as a result.
    tolls_asked: int = 0
    tolls_recovered: int = 0
    # Every reimbursementStatus seen, because the values have never been
    # observed unmasked and nothing here acts on them until they have been.
    statuses: list[str] = field(default_factory=list)


def _find(session: Session, invoice: TuroInvoice) -> ReimbursementInvoice | None:
    """The row the mail made for this invoice, if it made one.

    By Turo's id first — either of its ids, since the extension's own filings
    record the reimbursement id and the emails link the invoice id, and it has
    not been established whether those are the same number.

    Then, for an invoice the mail only ever showed as a total, by that total on
    the same reservation. Only rows with no lines: an itemised row has already
    said what it charged, and matching one on amount could pin this invoice's
    breakdown onto a different invoice that happens to cost the same.
    """
    ids = [i for i in (invoice.invoice_id, invoice.reimbursement_id) if i]
    by_id = session.scalar(
        select(ReimbursementInvoice).where(
            ReimbursementInvoice.reservation_id == invoice.reservation_id,
            ReimbursementInvoice.turo_invoice_id.in_(ids),
        )
    )
    if by_id is not None:
        return by_id
    totals = {invoice.total_cents}
    if invoice.total_before_fees_cents is not None:
        totals.add(invoice.total_before_fees_cents)
    candidates = [
        row
        for row in session.scalars(
            select(ReimbursementInvoice).where(
                ReimbursementInvoice.reservation_id == invoice.reservation_id,
                ReimbursementInvoice.total_cents.in_(totals),
            )
        )
        if not row.lines
    ]
    # Two unitemised invoices for the same amount on one rental cannot be told
    # apart, and guessing would give one of them the other's breakdown.
    return candidates[0] if len(candidates) == 1 else None


def apply_turo_invoice(
    session: Session, invoice: TuroInvoice, *, now: datetime, result: TuroInvoiceResult
) -> ReimbursementInvoice:
    result.seen += 1
    if invoice.status:
        result.statuses.append(f"{invoice.reservation_id}: {invoice.status}")
    row = _find(session, invoice)
    if row is None:
        row = ReimbursementInvoice(
            fingerprint=f"inv:{invoice.invoice_id}",
            reservation_id=invoice.reservation_id,
            turo_invoice_id=invoice.invoice_id,
            # Filed, whatever the status says. The status values have not been
            # read unmasked, and "charged" is the claim that ticks crossings
            # off as paid — not one to make from an enum nobody has seen. The
            # charged email, when it comes, moves it forward as usual.
            state=INVOICE_FILED,
            total_cents=invoice.total_cents,
            lines=[],
            last_seen_at=now,
        )
        session.add(row)
        result.created += 1
    else:
        result.matched += 1
        # Turo's invoice id, even over the reimbursement id the extension's own
        # filing recorded. It is the id the invoice hub lists, so keeping it
        # is what stops every pull from reading this invoice again — and what
        # lets the mail's row for it land here instead of beside it.
        row.turo_invoice_id = invoice.invoice_id
        row.last_seen_at = now

    had_toll_line = row.toll_cents is not None
    # Turo's own breakdown replaces the email's. It is typed, it is the whole
    # invoice, and the email's was a regular expression over whichever
    # notification happened to itemise.
    row.lines = [[line.title, line.cents] for line in invoice.lines]
    row.toll_cents = invoice.toll_cents

    if row.trip_id is None:
        trip = session.scalar(select(Trip).where(Trip.turo_trip_id == invoice.reservation_id))
        if trip is not None:
            row.trip_id = trip.id
    session.flush()

    if row.toll_cents is not None and not had_toll_line:
        result.newly_itemised.append(
            f"{invoice.reservation_id}: ${row.toll_cents / 100:,.2f} of "
            f"${row.total_cents / 100:,.2f} was tolls"
        )
        result.tolls_asked += _stamp_asked(session, row, now=now)
    elif row.toll_cents is None and not had_toll_line:
        result.newly_itemised.append(
            f"{invoice.reservation_id}: none of ${row.total_cents / 100:,.2f} was tolls"
        )

    if row.charged_at is not None and row.toll_cents is not None:
        recovered = ReimbursementResult()
        _recover(session, row, recovered)
        result.tolls_recovered += recovered.tolls_recovered
    return row


def _stamp_asked(session: Session, row: ReimbursementInvoice, *, now: datetime) -> int:
    """Mark as asked the crossings this invoice's toll line covered.

    The mail does this on an invoice's first sighting, for the crossings that
    existed then (`_mark_asked`). An invoice whose toll line is only learned
    now was asked at that first sighting all the same, so the crossings it can
    have covered are the ones imported by then — not every crossing on the
    rental today. One imported since was not part of the ask, and stamping it
    would strand it.
    """
    if row.trip_id is None:
        return 0
    stamped = 0
    for toll in session.scalars(
        select(Toll).where(
            Toll.trip_id == row.trip_id,
            Toll.filed_at.is_(None),
            Toll.recovered_at.is_(None),
            Toll.imported_at <= row.first_seen_at,
        )
    ):
        toll.filed_at = now
        stamped += 1
    return stamped


def wanted_invoices(session: Session, *, limit: int = 40) -> list[tuple[str, str]]:
    """Invoices worth reading from Turo: the ones the mail could not break down.

    Only those with an id to fetch by. An invoice seen only through the
    "charged" email has none, and is read when the operator opens its page and
    pulls from there instead.
    """
    rows = session.scalars(
        select(ReimbursementInvoice)
        .where(
            ReimbursementInvoice.turo_invoice_id.is_not(None),
            ReimbursementInvoice.toll_cents.is_(None),
        )
        .order_by(ReimbursementInvoice.last_seen_at.desc())
        .limit(limit * 4)
    ).all()
    out: list[tuple[str, str]] = []
    for row in rows:
        # An invoice that itemised and named no tolls has already answered.
        if not _unanswered(row):
            continue
        if row.turo_invoice_id:
            out.append((row.reservation_id, row.turo_invoice_id))
        if len(out) >= limit:
            break
    return out


@dataclass(frozen=True)
class HubInvoice:
    invoice_id: str
    status: str | None
    title: str | None


def parse_hub(body: Mapping[str, Any]) -> list[HubInvoice] | None:
    """The invoices `/api/reservations/<id>/invoice-hub` lists, or None.

    Observed on 59077848 as ``sections: [{type: RESOLVED, invoices: [{invoiceId,
    amount, status: PAID, title, type: INCIDENTAL, ...}]}]``. Only the ids are
    relied on: the breakdown comes from each invoice's own page, and the hub's
    amounts are integers where the invoice page's are dollars, which is a unit
    nobody has confirmed.
    """
    sections = body.get("sections")
    if not isinstance(sections, list):
        return None
    found: list[HubInvoice] = []
    for section in sections:
        if not isinstance(section, Mapping):
            continue
        invoices = section.get("invoices")
        if not isinstance(invoices, list):
            continue
        for entry in invoices:
            if not isinstance(entry, Mapping):
                continue
            invoice_id = _id(entry.get("invoiceId"))
            if invoice_id is None:
                continue
            status, title = entry.get("status"), entry.get("title")
            found.append(
                HubInvoice(
                    invoice_id=invoice_id,
                    status=status if isinstance(status, str) else None,
                    title=title if isinstance(title, str) else None,
                )
            )
    return found


def _unanswered(row: ReimbursementInvoice) -> bool:
    """Whether a row still cannot say how much of it was tolls."""
    if row.toll_cents is not None:
        return False
    labels = [entry[0] for entry in row.lines or [] if isinstance(entry, list) and entry]
    return not labels or any(isinstance(label, str) and names_tolls(label) for label in labels)


def hub_to_read(
    session: Session, reservation_id: str, listed: list[HubInvoice]
) -> list[tuple[str, str]]:
    """Which of a hub's invoices are worth fetching.

    One this app has no row for, under Turo's invoice id — which includes one
    the mail only ever showed as a total, since that row has no id — and one
    whose row still cannot say what share was tolls. An invoice already
    answered is not fetched again on every pull.
    """
    known = {
        row.turo_invoice_id: row
        for row in session.scalars(
            select(ReimbursementInvoice).where(
                ReimbursementInvoice.reservation_id == reservation_id,
                ReimbursementInvoice.turo_invoice_id.is_not(None),
            )
        )
    }
    out: list[tuple[str, str]] = []
    for invoice in listed:
        row = known.get(invoice.invoice_id)
        if row is None or _unanswered(row):
            out.append((reservation_id, invoice.invoice_id))
    return out
