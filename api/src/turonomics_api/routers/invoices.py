"""The bill, per rental, with the clock on it.

Separate from the tolls router because it answers a different question. That
one is the ledger — every crossing this account was charged for, whoever owes
it. This one is only what can still be collected, and from whom, soonest
deadline first.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from turonomics_api.db.base import get_session
from turonomics_api.db.models import ReimbursementInvoice, Toll, Trip
from turonomics_api.gmail.parse import names_tolls
from turonomics_api.ingest.evidence import EvidenceRow, EvidenceSheet, evidence_svg
from turonomics_api.ingest.invoices import Invoice, InvoiceLine, build_invoices
from turonomics_api.routers.tolls import require_token, token_configured
from turonomics_api.settings import toll_filing_window_days

log = logging.getLogger("turonomics.routers.invoices")

router = APIRouter(prefix="/api/invoices", tags=["invoices"])

DbSession = Annotated[Session, Depends(get_session)]

# An invoice this close to its deadline is the work for this week rather than
# this month. Three weeks: long enough to still be actionable when the operator
# sits down with the statement, short enough to mean something.
URGENT_DAYS = 21


class LineRow(BaseModel):
    toll_id: uuid.UUID
    occurred_at: datetime
    plaza: str
    amount_cents: int
    overrun_seconds: int | None = None


class InvoiceRow(BaseModel):
    trip_id: uuid.UUID
    guest_name: str | None
    vehicle_nickname: str | None
    starts_at: datetime
    ends_at: datetime
    off_platform: bool
    turo_trip_id: str | None
    total_cents: int
    lines: list[LineRow]
    # None off-platform: there is no Turo window to miss.
    file_by: datetime | None = None
    days_left: int | None = None
    expired: bool = False
    # What has already been asked of this guest through Turo, from the
    # reimbursement notifications. charged_cents is money that arrived;
    # pending_cents is filed but not yet paid. Both are shown because asking
    # twice for money already paid is a dispute, not income.
    charged_cents: int = 0
    pending_cents: int = 0
    # Set when an invoice was charged for this rental but its total does not
    # match these crossings — cleaning and fuel ride on the same invoices, so
    # it is surfaced rather than written off.
    charged_but_different: bool = False
    # What a charged invoice said it took for tolls specifically, where it
    # itemised. The figure worth comparing: a reimbursement bundles cleaning,
    # fuel and damage, so its total rarely equals a rental's crossings.
    charged_tolls_cents: int | None = None
    # Every charge on the invoices against this rental, as "label $amount",
    # so a bundled one reads as a bundle rather than as a puzzling total.
    charged_lines: list[str] = []


class InvoicesResponse(BaseModel):
    invoices: list[InvoiceRow]
    # Reimbursements already charged whose total matched nothing on file, so
    # the crossings they cover could not be ticked off automatically.
    needs_a_look_cents: int = 0
    window_days: int
    billable_cents: int
    # Still collectable, and soon. The figure to act on.
    urgent_cents: int
    # Past the window: a loss to acknowledge, not work to do. Reported
    # separately so it cannot quietly inflate what looks collectable.
    expired_cents: int
    off_platform_cents: int
    token_required: bool = False


def _reimbursements(
    session: Session, trip_ids: list[uuid.UUID]
) -> dict[uuid.UUID, list[ReimbursementInvoice]]:
    """Every reimbursement notification against these rentals, in one query."""
    if not trip_ids:
        return {}
    rows = session.scalars(
        select(ReimbursementInvoice).where(ReimbursementInvoice.trip_id.in_(trip_ids))
    ).all()
    out: dict[uuid.UUID, list[ReimbursementInvoice]] = {}
    for row in rows:
        if row.trip_id is not None:
            out.setdefault(row.trip_id, []).append(row)
    return out


def _unreadable(rows: list[ReimbursementInvoice]) -> int:
    """Money Turo has asked for on a rental without saying whether it was tolls.

    Two shapes, and both mean the same thing for filing — nobody here can say
    whether these crossings were part of it:

    * no lines at all. The "has been charged" email links the receipt rather
      than the invoice and often does not itemise, so an invoice seen only
      through that email is a total and nothing else. Austin's $140.40 on
      59077848 is one, beside a $50.00 ticket that did itemise.
    * a line that names tolls without being a toll line of its own — "Tolls
      and fuel", or two toll lines.

    An invoice with a readable toll line is not here: `_mark_asked` has already
    stamped the crossings it covered. Nor is one that itemised only tickets or
    refuelling, which asked for nothing to do with tolls.

    Next-draft skips a rental with any of this, because the alternative is
    filing $40.71 of crossings that the $140.40 may already have charged. The
    cost of skipping is a rental that waits for a person to read Turo's invoice
    page; the cost of not skipping is a guest asked twice.
    """
    total = 0
    for row in rows:
        if row.toll_cents is not None:
            continue
        labels = [
            entry[0]
            for entry in row.lines or []
            if isinstance(entry, list) and len(entry) == 2 and isinstance(entry[0], str)
        ]
        if not labels or any(names_tolls(label) for label in labels):
            total += row.total_cents
    return total


def _row(
    invoice: Invoice,
    window: int,
    now: datetime,
    asked: list[ReimbursementInvoice] | None = None,
) -> InvoiceRow:
    left = invoice.days_left(window, now)
    asked = asked or []
    charged = sum(r.total_cents for r in asked if r.state == "charged")
    pending = sum(r.total_cents for r in asked if r.state != "charged")
    charged_rows = [r for r in asked if r.state == "charged"]
    toll_lines = [r.toll_cents for r in charged_rows if r.toll_cents is not None]
    charged_tolls = sum(toll_lines) if toll_lines else None
    lines: list[str] = []
    for row in charged_rows:
        for entry in row.lines or []:
            # Narrowed rather than cast: this came out of a JSONB column, so
            # the shape is whatever was written, and a row from an older
            # version of the writer is a real possibility rather than a
            # type-checker formality.
            if not (isinstance(entry, list) and len(entry) == 2):
                continue
            label, cents = entry
            if isinstance(label, str) and isinstance(cents, int):
                lines.append(f"{label} ${cents / 100:,.2f}")
    return InvoiceRow(
        trip_id=invoice.trip_id,
        guest_name=invoice.guest_name,
        vehicle_nickname=invoice.vehicle_nickname,
        starts_at=invoice.starts_at,
        ends_at=invoice.ends_at,
        off_platform=invoice.off_platform,
        turo_trip_id=invoice.turo_trip_id,
        total_cents=invoice.total_cents,
        lines=[
            LineRow(
                toll_id=line.toll_id,
                occurred_at=line.occurred_at,
                plaza=line.plaza,
                amount_cents=line.amount_cents,
                overrun_seconds=line.overrun_seconds,
            )
            for line in invoice.lines
        ],
        file_by=invoice.file_by(window),
        days_left=left,
        expired=left is not None and left < 0,
        charged_cents=charged,
        pending_cents=pending,
        # Only interesting while something is still outstanding: once the
        # crossings are ticked off they leave this list anyway.
        charged_tolls_cents=charged_tolls,
        charged_lines=lines,
        # Compared on the toll line where the invoice itemised one, since that
        # is the part that can be reconciled at all.
        charged_but_different=bool(charged)
        and (charged_tolls if charged_tolls is not None else charged) != invoice.total_cents,
    )


class DraftLine(BaseModel):
    toll_id: uuid.UUID
    occurred_at: datetime
    plaza: str
    amount_cents: int


class DraftResponse(BaseModel):
    """Everything needed to file one toll invoice, assembled in one place.

    The extension is what talks to Turo, and it should not have to decide what
    to claim. This says which reservation, which crossings, how much, whether
    Turo will still take it, and carries the evidence sheet to attach.
    """

    trip_id: uuid.UUID
    turo_trip_id: str | None
    guest_name: str | None
    vehicle_nickname: str | None
    starts_at: datetime
    ends_at: datetime
    lines: list[DraftLine]
    total_cents: int
    days_left: int | None
    # Turo's own answer where the pull has fetched it, and the 90-day window
    # otherwise. Named so the caller can tell which it got, because one is a
    # fact about this reservation and the other is arithmetic.
    can_file: bool
    # Turo's `allowedToRequestReimbursement`, reported and *not* acted on.
    #
    # It was briefly a hard gate here, on the assumption that it meant "a
    # reimbursement may still be requested". Against the live account it is
    # false for all 37 rentals — including the one that was then filed by hand
    # and charged to the guest. Whatever it means, it is not that, and a gate
    # built on it blocked every invoice this app could otherwise raise.
    turo_allows_request: bool | None
    # Turo's filing API takes dollars, where everything in this codebase is
    # integer cents. Converted once, here, rather than in the extension — a
    # rounding decision about money belongs where it can be tested.
    amount_dollars: float
    # The note that goes to the guest. Written here for the same reason: it is
    # the only part of a filing a person reads, and it should not vary with
    # whoever happens to be clicking the button.
    message: str
    # The evidence to attach, as SVG. The extension rasterises it: Turo wants
    # an image, and a browser is already the thing holding one.
    evidence_svg: str
    # Whether the ledger would file this rental, and if not, why not. A draft
    # is also a preview — the site shows one for any rental — so it is drafted
    # either way, and the extension refuses to file one that is held.
    fileable: bool = True
    held_because: str | None = None


def _note(lines: list[InvoiceLine]) -> str:
    """What the guest reads.

    Plain, specific, and free of anything that reads as an accusation: the
    crossings are a fact, and a sentence about them does not need to imply the
    guest was doing anything other than driving the car they rented.
    """
    count = len(lines)
    total = sum(line.amount_cents for line in lines)
    crossings = "toll" if count == 1 else "tolls"
    return (
        f"{count} {crossings} on your trip, totalling "
        f"${total / 100:,.2f}. The attached sheet lists each one "
        f"with its time and plaza, taken from the vehicle's E-ZPass account. "
        f"Happy to send the statement itself if you would like it."
    )


def _unfiled(session: Session, invoice: Invoice) -> list[InvoiceLine]:
    """The crossings on this rental that have not been asked for.

    Per crossing, not per rental. A statement that arrives late can add a
    crossing to a rental already invoiced, and that crossing is still owed —
    while the ones beside it, already asked for, must not be asked for twice.
    """
    filed = {
        row
        for row in session.scalars(
            select(Toll.id).where(
                Toll.id.in_([line.toll_id for line in invoice.lines]),
                Toll.filed_at.is_not(None),
            )
        )
    }
    return [line for line in invoice.lines if line.toll_id not in filed]


def _draft(session: Session, invoice: Invoice, now: datetime, window: int) -> DraftResponse:
    lines = _unfiled(session, invoice)
    if not lines:
        raise HTTPException(
            status_code=404, detail="every crossing on that rental has been asked for"
        )
    total_cents = sum(line.amount_cents for line in lines)
    trip = session.get(Trip, invoice.trip_id)
    from_turo = trip.can_file_reimbursement if trip is not None else None
    left = invoice.days_left(window, now)
    sheet = EvidenceSheet(
        reservation_id=invoice.turo_trip_id,
        guest_name=invoice.guest_name,
        vehicle=invoice.vehicle_nickname or "the car",
        plate=trip.vehicle.plate if trip is not None and trip.vehicle else None,
        starts_at=invoice.starts_at,
        ends_at=invoice.ends_at,
        rows=[
            EvidenceRow(
                occurred_at=line.occurred_at, plaza=line.plaza, amount_cents=line.amount_cents
            )
            for line in lines
        ],
        imported_at=trip.detail_synced_at if trip is not None else None,
    )
    return DraftResponse(
        trip_id=invoice.trip_id,
        turo_trip_id=invoice.turo_trip_id,
        guest_name=invoice.guest_name,
        vehicle_nickname=invoice.vehicle_nickname,
        starts_at=invoice.starts_at,
        ends_at=invoice.ends_at,
        lines=[
            DraftLine(
                toll_id=line.toll_id,
                occurred_at=line.occurred_at,
                plaza=line.plaza,
                amount_cents=line.amount_cents,
            )
            for line in lines
        ],
        total_cents=total_cents,
        # Cents to dollars once, at the boundary. int / 100 is exact for any
        # cent total, and the alternative — the extension dividing — is the
        # same arithmetic somewhere nothing checks it.
        amount_dollars=total_cents / 100,
        message=_note(lines),
        days_left=left,
        # The window decides. Turo's flag is reported beside it and does not
        # override it — see `turo_allows_request`.
        can_file=left is not None and left >= 0,
        turo_allows_request=from_turo,
        evidence_svg=evidence_svg(sheet),
    )


class LedgerRow(BaseModel):
    """One rental, with both ledgers side by side."""

    trip_id: uuid.UUID
    turo_trip_id: str | None
    guest_name: str | None
    vehicle_nickname: str | None
    starts_at: datetime
    ends_at: datetime
    days_left: int | None

    # What this app worked out from the statements, and what became of it.
    # These three sum to `tolls_cents`.
    tolls_cents: int
    unfiled_cents: int
    filed_cents: int
    recovered_cents: int

    # What Turo says, which is a different ledger rather than a check on the
    # same one: its totals bundle refuelling and tickets, and only the toll
    # line is comparable.
    asked_cents: int
    charged_cents: int
    turo_toll_line_cents: int | None

    # Turo's `allowedToRequestReimbursement`, reported and not acted on: it is
    # false for every rental on the live account, including ones that were
    # filed successfully afterwards.
    turo_allows_request: bool | None
    # A word for the row, so a page does not have to re-derive one and two
    # readers do not reach different conclusions from the same numbers.
    state: str
    # Set when the two ledgers disagree in a way worth a person's attention.
    note: str | None = None


class LedgerResponse(BaseModel):
    rows: list[LedgerRow]
    tolls_cents: int
    unfiled_cents: int
    filed_cents: int
    recovered_cents: int
    charged_cents: int


def _ledger_state(
    *,
    tolls: int,
    unfiled: int,
    filed: int,
    recovered: int,
    toll_line: int | None,
    left: int | None,
    unreadable: int = 0,
) -> tuple[str, str | None]:
    """What this rental's crossings amount to, in a word.

    Ordered by what a person would do about it, not by the data: the rows
    worth acting on are the ones where money is still collectable.
    """
    if tolls == 0:
        return "no crossings", None
    if recovered == tolls:
        return "settled", None
    if unfiled == 0 and filed > 0:
        return "awaiting payment", None
    if unfiled > 0 and unreadable > 0:
        # Before every state that would offer these crossings for filing,
        # because each of them would be offering money that may already have
        # been charged. See `_unreadable`.
        return (
            "check Turo's invoice",
            f"Turo has asked for ${unreadable / 100:,.2f} on this rental without "
            f"saying what for — read its invoice before asking for "
            f"${unfiled / 100:,.2f} of tolls",
        )
    if unfiled > 0 and (filed > 0 or recovered > 0):
        # The case the per-crossing tracking exists for: a statement arriving
        # after the first invoice went out.
        #
        # `unfiled > 0` here survives mutation and cannot be tested away: the
        # three columns sum to the rental's tolls, so `unfiled == 0` with
        # anything filed is already "awaiting payment" above, and with nothing
        # filed it is "settled". It stays as a statement of what this branch
        # means, since the arithmetic that makes it redundant lives elsewhere
        # and could change.
        #
        # Turo having charged something is deliberately *not* part of this
        # condition, and including it was wrong on real data: six rentals read
        # as "partly billed" with nothing ever filed, because Turo had charged
        # them for refuelling or a ticket. Its invoice totals say nothing about
        # whose tolls are outstanding — only its toll line does, and these had
        # none.
        return (
            "partly billed",
            f"${unfiled / 100:,.2f} of these crossings has not been asked for",
        )
    if unfiled > 0 and toll_line is not None:
        # Turo charged a toll line of its own and recovery could not reconcile
        # it, so somebody has billed for tolls on this rental and it was not
        # this app. Worth a person's eye before asking the guest again.
        return (
            "check Turo's toll line",
            f"Turo charged ${toll_line / 100:,.2f} of tolls against "
            f"${unfiled / 100:,.2f} still outstanding here",
        )
    if unfiled > 0 and left is not None and left < 0:
        return "expired", "past the 90-day window, so this cannot be filed"
    if unfiled > 0:
        return "to bill", None
    return "settled", None


@dataclass(frozen=True)
class _Tally:
    unfiled: int
    filed: int
    recovered: int
    left: int | None
    state: str
    note: str | None


def _tally(
    session: Session,
    invoice: Invoice,
    theirs: list[ReimbursementInvoice],
    *,
    window: int,
    now: datetime,
) -> _Tally:
    """One rental's three columns and the word for them.

    Shared by the ledger and by next-draft, so that the button only ever files
    what the ledger calls to bill. They were separate once, and next-draft
    offered Austin's $40.71 while the ledger — had it been able to see the
    $140.40 — would have said to check Turo first.
    """
    tolls = session.scalars(
        select(Toll).where(Toll.id.in_([line.toll_id for line in invoice.lines]))
    ).all()
    unfiled = sum(t.amount_cents for t in tolls if t.recovered_at is None and t.filed_at is None)
    filed = sum(
        t.amount_cents for t in tolls if t.recovered_at is None and t.filed_at is not None
    )
    recovered = sum(t.amount_cents for t in tolls if t.recovered_at is not None)
    toll_lines = [r.toll_cents for r in theirs if r.toll_cents is not None]
    left = invoice.days_left(window, now)
    state, note = _ledger_state(
        tolls=invoice.total_cents,
        unfiled=unfiled,
        filed=filed,
        recovered=recovered,
        toll_line=sum(toll_lines) if toll_lines else None,
        left=left,
        unreadable=_unreadable(theirs),
    )
    return _Tally(unfiled, filed, recovered, left, state, note)


# The only states next-draft files from. Everything else is either done, out
# of time, or waiting for a person to look at what Turo already charged.
_FILEABLE = frozenset({"to bill", "partly billed"})


@router.get("/ledger", response_model=LedgerResponse)
def ledger(session: DbSession) -> LedgerResponse:
    """Every rental with crossings, and what has become of each.

    Deliberately separate from the invoices list, which answers "what should I
    do next" and so leaves out everything already dealt with. This answers
    "where did it all go", which needs the settled ones in it or the totals do
    not add up.
    """
    now = datetime.now(UTC)
    window = toll_filing_window_days()
    built = build_invoices(session, now=now, include_recovered=True)
    asked = _reimbursements(session, [i.trip_id for i in built])

    rows: list[LedgerRow] = []
    for invoice in built:
        theirs = asked.get(invoice.trip_id) or []
        tally = _tally(session, invoice, theirs, window=window, now=now)
        unfiled, filed, recovered = tally.unfiled, tally.filed, tally.recovered
        state, note, left = tally.state, tally.note, tally.left
        trip = session.get(Trip, invoice.trip_id)
        charged = sum(r.total_cents for r in theirs if r.state == "charged")
        toll_lines = [r.toll_cents for r in theirs if r.toll_cents is not None]
        rows.append(
            LedgerRow(
                trip_id=invoice.trip_id,
                turo_trip_id=invoice.turo_trip_id,
                guest_name=invoice.guest_name,
                vehicle_nickname=invoice.vehicle_nickname,
                starts_at=invoice.starts_at,
                ends_at=invoice.ends_at,
                days_left=left,
                tolls_cents=invoice.total_cents,
                unfiled_cents=unfiled,
                filed_cents=filed,
                recovered_cents=recovered,
                asked_cents=sum(r.total_cents for r in theirs),
                charged_cents=charged,
                turo_toll_line_cents=sum(toll_lines) if toll_lines else None,
                turo_allows_request=(
                    trip.can_file_reimbursement if trip is not None else None
                ),
                state=state,
                note=note,
            )
        )
    rows.sort(key=lambda row: row.ends_at, reverse=True)
    return LedgerResponse(
        rows=rows,
        tolls_cents=sum(r.tolls_cents for r in rows),
        unfiled_cents=sum(r.unfiled_cents for r in rows),
        filed_cents=sum(r.filed_cents for r in rows),
        recovered_cents=sum(r.recovered_cents for r in rows),
        charged_cents=sum(r.charged_cents for r in rows),
    )


@router.get("/next-draft", response_model=DraftResponse)
def next_draft(session: DbSession) -> DraftResponse:
    """The rental most worth filing for, drafted.

    Which one that is belongs here rather than in the extension: it is a
    judgement about money and deadlines, and the extension is a browser plugin
    that has to be rebuilt and side-loaded to change.

    Soonest deadline first, because that is the one about to be lost. Rentals
    Turo has already been asked about are skipped — asking twice for the same
    crossings is the mistake this whole feature exists to avoid.
    """
    now = datetime.now(UTC)
    window = toll_filing_window_days()
    built = build_invoices(session, now=now)
    asked = _reimbursements(session, [i.trip_id for i in built])
    fileable = []
    for invoice in built:
        tally = _tally(
            session, invoice, asked.get(invoice.trip_id) or [], window=window, now=now
        )
        # The ledger's word decides, not a second copy of its reasoning. That
        # covers the window, Turo having charged something unreadable, and
        # Turo having charged a toll line none of these crossings were stamped
        # against — each of which is a guest who may be asked twice.
        if tally.state not in _FILEABLE or tally.left is None:
            continue
        # Per crossing, not per rental. Skipping any rental that carried a
        # reimbursement was safe against asking twice and wrong the other way:
        # a crossing landing on a later statement, for a rental already
        # invoiced, could never be asked for at all.
        unfiled = _unfiled(session, invoice)
        if not unfiled:
            continue
        fileable.append((tally.left, -sum(line.amount_cents for line in unfiled), invoice))
    if not fileable:
        raise HTTPException(status_code=404, detail="nothing to file")
    fileable.sort(key=lambda row: (row[0], row[1]))
    return _draft(session, fileable[0][2], now, window)


@router.get("/{trip_id}/draft", response_model=DraftResponse)
def draft(trip_id: uuid.UUID, session: DbSession) -> DraftResponse:
    now = datetime.now(UTC)
    window = toll_filing_window_days()
    built = build_invoices(session, now=now)
    invoice = next((i for i in built if i.trip_id == trip_id), None)
    if invoice is None:
        # 404 rather than an empty draft: filing nothing is not a thing to do,
        # and a rental whose crossings are all recovered has no invoice to
        # draft rather than an invoice for zero.
        raise HTTPException(status_code=404, detail="no outstanding crossings on that rental")
    out = _draft(session, invoice, now, window)
    held = hold_reason(session, invoice, now=now, window=window)
    out.fileable = held is None
    out.held_because = held
    return out


def hold_reason(
    session: Session, invoice: Invoice, *, now: datetime, window: int
) -> str | None:
    """Why the ledger would not file this rental, or None if it would.

    The same rule next-draft applies, for a rental somebody picked by hand —
    from the site's File button, which must not be a way around it.
    """
    asked = _reimbursements(session, [invoice.trip_id]).get(invoice.trip_id) or []
    tally = _tally(session, invoice, asked, window=window, now=now)
    if tally.state not in _FILEABLE:
        return tally.note or tally.state
    if tally.left is None:
        return "off-platform, so there is nothing to file on Turo"
    if not _unfiled(session, invoice):
        return "every crossing on it has been asked for"
    return None


def fileable_invoice(session: Session, trip_id: uuid.UUID) -> tuple[Invoice | None, str | None]:
    """The rental's invoice and why it cannot be filed, for a caller outside."""
    now = datetime.now(UTC)
    window = toll_filing_window_days()
    invoice = next((i for i in build_invoices(session, now=now) if i.trip_id == trip_id), None)
    if invoice is None:
        return None, "no outstanding crossings on that rental"
    return invoice, hold_reason(session, invoice, now=now, window=window)


class FiledIn(BaseModel):
    """What Turo said when it took the filing."""

    reimbursement_id: int
    amount_cents: int


class FiledResponse(BaseModel):
    recorded: bool
    fingerprint: str


@router.post("/{trip_id}/filed", response_model=FiledResponse)
def filed(
    trip_id: uuid.UUID,
    payload: FiledIn,
    session: DbSession,
    authorization: Annotated[str | None, Header()] = None,
) -> FiledResponse:
    """Record that an invoice was filed, so nothing asks for it twice.

    The mail sync would find this eventually — Turo emails about every
    reimbursement — but "eventually" is up to ten minutes, and in that window
    the page still lists the crossings as money to collect. Filing the same
    tolls twice is a dispute with a guest, which is the expensive end of this
    whole feature.

    Written with the same fingerprint scheme the mail parser uses, so when the
    email does arrive it lands on this row rather than beside it.
    """
    require_token(authorization)
    trip = session.get(Trip, trip_id)
    if trip is None or not trip.turo_trip_id:
        raise HTTPException(status_code=404, detail="no such rental")
    fingerprint = f"inv:{payload.reimbursement_id}"
    existing = session.scalar(
        select(ReimbursementInvoice).where(ReimbursementInvoice.fingerprint == fingerprint)
    )
    if existing is not None:
        # Idempotent: a retried post, or the email having arrived first.
        return FiledResponse(recorded=False, fingerprint=fingerprint)
    now = datetime.now(UTC)
    # Stamp the crossings themselves, so a later statement adding one to this
    # rental can still be asked for while these cannot be asked for twice.
    stamped = 0
    for toll in session.scalars(
        select(Toll).where(Toll.trip_id == trip.id, Toll.filed_at.is_(None))
    ):
        toll.filed_at = now
        stamped += 1
    session.add(
        ReimbursementInvoice(
            fingerprint=fingerprint,
            reservation_id=trip.turo_trip_id,
            turo_invoice_id=str(payload.reimbursement_id),
            guest_name=trip.guest_name,
            # Filed, not charged. The guest has been asked and has not paid,
            # and treating the ask as the payment is how a crossing silently
            # stops being chased.
            state="filed",
            total_cents=payload.amount_cents,
            lines=[["Tolls", payload.amount_cents]],
            toll_cents=payload.amount_cents,
            trip_id=trip.id,
            last_seen_at=now,
        )
    )
    session.commit()
    log.info(
        "filed reimbursement %s for reservation %s: %dc across %d crossing(s)",
        payload.reimbursement_id,
        trip.turo_trip_id,
        payload.amount_cents,
        stamped,
    )
    return FiledResponse(recorded=True, fingerprint=fingerprint)


@router.get("", response_model=InvoicesResponse)
def list_invoices(session: DbSession, include_recovered: bool = False) -> InvoicesResponse:
    now = datetime.now(UTC)
    window = toll_filing_window_days()
    built = build_invoices(session, now=now, include_recovered=include_recovered)
    asked = _reimbursements(session, [i.trip_id for i in built])
    rows = [_row(i, window, now, asked.get(i.trip_id)) for i in built]
    return InvoicesResponse(
        invoices=rows,
        window_days=window,
        billable_cents=sum(r.total_cents for r in rows),
        urgent_cents=sum(
            r.total_cents for r in rows
            if r.days_left is not None and 0 <= r.days_left <= URGENT_DAYS
        ),
        expired_cents=sum(r.total_cents for r in rows if r.expired),
        needs_a_look_cents=sum(r.total_cents for r in rows if r.charged_but_different),
        off_platform_cents=sum(r.total_cents for r in rows if r.off_platform),
        token_required=token_configured(),
    )


@router.post("/{trip_id}/recovered", response_model=InvoiceRow)
def mark_invoice_recovered(
    trip_id: uuid.UUID,
    session: DbSession,
    undo: bool = False,
    authorization: Annotated[str | None, Header()] = None,
) -> InvoiceRow:
    """Tick off a whole invoice once it has been filed.

    One call rather than twenty: a guest with twenty crossings is one bill, and
    ticking it crossing by crossing is how a row gets missed.
    """
    require_token(authorization)
    tolls = session.scalars(select(Toll).where(Toll.trip_id == trip_id)).all()
    if not tolls:
        raise HTTPException(404, "no crossings are attributed to that rental")
    stamp = None if undo else datetime.now(UTC)
    for toll in tolls:
        toll.recovered_at = stamp
    session.commit()

    now = datetime.now(UTC)
    window = toll_filing_window_days()
    # Rebuilt including recovered crossings, so the response describes the
    # invoice that was just ticked rather than an empty one.
    for invoice in build_invoices(session, now=now, include_recovered=True):
        if invoice.trip_id == trip_id:
            return _row(invoice, window, now, _reimbursements(session, [trip_id]).get(trip_id))
    raise HTTPException(404, "no crossings are attributed to that rental")
