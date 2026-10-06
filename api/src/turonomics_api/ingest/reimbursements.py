"""Money already asked of a guest, so the page does not ask twice.

Turo sends the host three notifications about one reimbursement invoice: when
it is filed, when the guest has not responded, and when they have been
charged. None of them is a trip email — no dates, no reservation block — so the
trip parser rightly refuses them, and before this they were counted as "not a
trip" and discarded.

Discarding them is expensive in one specific direction. A crossing already
charged through a reimbursement is not money waiting to be collected, and
listing it as such does not merely overstate a total: it invites a second
invoice for money the guest has already paid, which is a dispute rather than
income.

What is deliberately *not* done here is attribute by amount alone. An invoice
total that matches a rental's tolls to the cent is taken as being those tolls;
anything else is recorded and surfaced, and left for a person. An invoice can
cover cleaning or fuel as easily as tolls, and guessing in that direction
writes off money nobody has paid.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from turonomics_api.db.models import ReimbursementInvoice, Toll, Trip
from turonomics_api.gmail.parse import INVOICE_CHARGED, ParsedInvoice, names_tolls

log = logging.getLogger("turonomics.ingest.reimbursements")

# Which state supersedes which. An invoice can only move forwards: a "filed"
# notification arriving after a "charged" one (mail is not ordered) must not
# un-charge it.
_RANK = {"filed": 0, "unanswered": 1, INVOICE_CHARGED: 2}


@dataclass
class ReimbursementResult:
    seen: int = 0
    created: int = 0
    updated: int = 0
    # Crossings marked recovered because an invoice covering them was charged.
    tolls_recovered: int = 0
    # Charged invoices whose total did not match any rental's tolls. Reported
    # rather than guessed at.
    unmatched_totals: list[str] = field(default_factory=list)


def _toll_total(session: Session, trip_id: uuid.UUID | None) -> int:
    return int(
        session.scalar(
            select(func.coalesce(func.sum(Toll.amount_cents), 0)).where(
                Toll.trip_id == trip_id
            )
        )
        or 0
    )


def record_invoice(
    session: Session,
    parsed: ParsedInvoice,
    *,
    now: datetime,
    result: ReimbursementResult | None = None,
) -> ReimbursementInvoice:
    """Store or update one invoice, and recover its tolls if it was charged."""
    result = result if result is not None else ReimbursementResult()
    result.seen += 1

    invoice = session.scalar(
        select(ReimbursementInvoice).where(
            ReimbursementInvoice.fingerprint == parsed.fingerprint
        )
    )
    if invoice is None and parsed.turo_invoice_id:
        # Read off Turo's invoice page before the mail arrived, or filed by
        # the extension and since matched to its invoice id. Either way it is
        # this invoice, and a second row for it would count the money twice.
        invoice = session.scalar(
            select(ReimbursementInvoice).where(
                ReimbursementInvoice.reservation_id == parsed.reservation_id,
                ReimbursementInvoice.turo_invoice_id == parsed.turo_invoice_id,
            )
        )
    is_new = False
    if invoice is None:
        invoice = ReimbursementInvoice(
            fingerprint=parsed.fingerprint,
            reservation_id=parsed.reservation_id,
            turo_invoice_id=parsed.turo_invoice_id,
            guest_name=parsed.guest_name,
            state=parsed.state,
            total_cents=parsed.total_cents,
            lines=[[label, cents] for label, cents in parsed.lines],
            toll_cents=parsed.toll_cents,
            last_seen_at=now,
        )
        session.add(invoice)
        result.created += 1
        is_new = True
    else:
        # Forwards only. Mail arrives out of order often enough that a "filed"
        # notification can land after the "charged" one, and treating that as
        # news would make collected money look outstanding again.
        if _RANK.get(parsed.state, 0) > _RANK.get(invoice.state, 0):
            invoice.state = parsed.state
            result.updated += 1
        invoice.last_seen_at = now
        # The three notifications of one invoice do not all itemise. Keep
        # whichever sighting said the most rather than the most recent.
        if parsed.lines and not invoice.lines:
            invoice.lines = [[label, cents] for label, cents in parsed.lines]
        if parsed.toll_cents is not None and invoice.toll_cents is None:
            invoice.toll_cents = parsed.toll_cents
        invoice.turo_invoice_id = invoice.turo_invoice_id or parsed.turo_invoice_id
        invoice.guest_name = invoice.guest_name or parsed.guest_name

    if invoice.state == INVOICE_CHARGED and invoice.charged_at is None:
        invoice.charged_at = now

    # The rental, if this fleet has one for that reservation. An invoice can
    # arrive before the trip mail does, so this is retried on every sighting
    # rather than only at creation.
    if invoice.trip_id is None:
        trip = session.scalar(
            select(Trip).where(Trip.turo_trip_id == parsed.reservation_id)
        )
        if trip is not None:
            invoice.trip_id = trip.id
    session.flush()

    _mark_asked(session, invoice, now=now, first_sighting=is_new)

    if invoice.state == INVOICE_CHARGED and invoice.trip_id is not None:
        _recover(session, invoice, result)
    return invoice


def _mark_asked(
    session: Session,
    invoice: ReimbursementInvoice,
    *,
    now: datetime,
    first_sighting: bool,
) -> None:
    """Record that this rental's crossings have been asked for.

    The extension stamps `filed_at` when it files. An invoice filed any other
    way — by hand in Turo's own UI — reaches this app only as a notification
    email, and until this existed nothing stamped the crossings: the button
    would then offer the same rental again, which is a guest asked twice for
    the same tolls.

    Only on the first sighting of an invoice, and only crossings that exist at
    that moment. The same email is re-read on every sync, and a crossing that
    arrives on a later statement was not part of the ask — stamping it on a
    re-read would strand it, which is the bug per-crossing tracking was built
    to fix.

    Only an invoice with a toll line. One that charged for refuelling or a
    ticket asked the guest for nothing to do with these crossings.
    """
    if not first_sighting or invoice.trip_id is None or invoice.toll_cents is None:
        return
    for toll in session.scalars(
        select(Toll).where(
            Toll.trip_id == invoice.trip_id,
            Toll.filed_at.is_(None),
            Toll.recovered_at.is_(None),
        )
    ):
        toll.filed_at = now


def _recover(
    session: Session, invoice: ReimbursementInvoice, result: ReimbursementResult
) -> None:
    """Mark a rental's crossings recovered, if this invoice plainly charged them.

    Plainly means two things, and only together: the invoice says on a line of
    its own what it charged for tolls, and that line is at least what this
    fleet has attributed to the rental.

    The first half is what keeps a ticket from paying off a toll. The second is
    a deliberate asymmetry — covering too little leaves money uncollected and
    is reported; covering what was never charged bills a guest twice, which is
    a dispute rather than income.
    """
    if invoice.state != INVOICE_CHARGED or invoice.charged_at is None:
        # Both guards survive mutation, and the reason is worth knowing rather
        # than papering over: `recovered_at = invoice.charged_at` below is a
        # no-op when charged_at is None, so removing either check changes
        # nothing observable. What actually keeps a filed invoice from writing
        # off a crossing is the invariant that charged_at is set only when the
        # state is charged — which `test_charged_at_is_set_only_when_charged`
        # pins, because it is the load-bearing part.
        #
        # These stay as a statement of intent. A later change that stamped
        # recovered_at from anything else would need them.
        return

    total = _toll_total(session, invoice.trip_id)
    if total == 0:
        # A rental with no crossings is not a mismatch. Most reimbursement
        # invoices are for cleaning or fuel on trips with no tolls at all, and
        # reporting each one as "did not match" would bury the handful that are
        # worth looking at.
        return

    asked = invoice.toll_cents
    if asked is None and invoice.lines:
        # Itemised, and no single readable toll line. The total is emphatically
        # not a substitute here, and this is the case that used to be able to
        # write off the wrong thing: "Tickets - $50.00" on a rental with
        # exactly $50.00 of crossings matched on the total and ticked them off
        # against a parking fine. An itemised invoice has already said what it
        # charged for; if tolls are not on it, it did not charge for tolls.
        if any(
            isinstance(entry, list)
            and len(entry) == 2
            and isinstance(entry[0], str)
            and names_tolls(entry[0])
            for entry in invoice.lines
        ):
            # "Tolls and fuel", or two toll lines: it charged *something* for
            # tolls and will not say how much.
            result.unmatched_totals.append(
                f"{invoice.reservation_id}: names tolls but not on a line of its "
                f"own, against {total}c of crossings"
            )
        # Otherwise: a cleaning or refuelling invoice on a rental that happens
        # to have crossings too. Nothing to reconcile and nothing wrong, so it
        # is not reported either — six of the eight charged invoices on the
        # live account are this, and reporting them would bury the rest.
        return

    if asked is None:
        # Nothing itemised at all, so the total is the only number there is.
        # Weak evidence, and held to the strictest test because of it: equal to
        # the cent or nothing. The real invoices all itemise, so reaching this
        # means a notification shape that does not — and being wrong here is
        # bounded by that coincidence.
        if invoice.total_cents != total:
            result.unmatched_totals.append(
                f"{invoice.reservation_id}: invoice total {invoice.total_cents}c "
                f"vs tolls {total}c"
            )
            return
    elif asked < total:
        # Part of the crossings, or a toll this fleet has not imported. Either
        # way some of the money is still owed and guessing which part is not
        # this function's business.
        result.unmatched_totals.append(
            f"{invoice.reservation_id}: toll line {asked}c covers only part of {total}c"
        )
        return

    # At least everything attributed to this rental. Equal is the clean case;
    # more happens because Turo bills tolls this fleet has no statement for —
    # the July rentals were charged $25.00 and $27.04 against $9.79 and $16.79
    # of imported crossings. Refusing those left money the guest had already
    # paid sitting on the page as owed, which is the mistake that invites a
    # second invoice.
    if asked is not None and asked > total:
        log.info(
            "reimbursement %s charged %dc of tolls, %dc more than reservation %s "
            "has crossings for",
            invoice.turo_invoice_id or invoice.fingerprint,
            asked,
            asked - total,
            invoice.reservation_id,
        )
    outstanding = session.scalars(
        select(Toll).where(Toll.trip_id == invoice.trip_id, Toll.recovered_at.is_(None))
    ).all()
    for toll in outstanding:
        toll.recovered_at = invoice.charged_at
    if outstanding:
        result.tolls_recovered += len(outstanding)
        log.info(
            "reimbursement %s covered %d crossing(s) on reservation %s",
            invoice.turo_invoice_id or invoice.fingerprint,
            len(outstanding),
            invoice.reservation_id,
        )


def relink_invoices(session: Session, *, now: datetime) -> ReimbursementResult:
    """Retry invoices whose rental or crossings were not there at the time.

    An invoice can arrive before the trip mail, and a statement is imported
    long after both. Neither is a reason for the money to stay listed as
    outstanding, so this is run after a mail sync and after a toll import.
    """
    result = ReimbursementResult()
    invoices = session.scalars(
        select(ReimbursementInvoice).where(
            ReimbursementInvoice.state == INVOICE_CHARGED
        )
    ).all()
    for invoice in invoices:
        result.seen += 1
        if invoice.trip_id is None:
            trip = session.scalar(
                select(Trip).where(Trip.turo_trip_id == invoice.reservation_id)
            )
            if trip is None:
                continue
            invoice.trip_id = trip.id
            session.flush()
        if invoice.charged_at is None:
            invoice.charged_at = now
        _recover(session, invoice, result)
    return result
