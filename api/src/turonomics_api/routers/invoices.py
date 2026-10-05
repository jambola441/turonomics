"""The bill, per rental, with the clock on it.

Separate from the tolls router because it answers a different question. That
one is the ledger — every crossing this account was charged for, whoever owes
it. This one is only what can still be collected, and from whom, soonest
deadline first.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from turonomics_api.db.base import get_session
from turonomics_api.db.models import ReimbursementInvoice, Toll
from turonomics_api.ingest.invoices import Invoice, build_invoices
from turonomics_api.routers.tolls import require_token, token_configured
from turonomics_api.settings import toll_filing_window_days

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
