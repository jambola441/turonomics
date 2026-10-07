"""What the extension pulls from Turo's own API, and where it goes.

Two endpoints, deliberately shaped so the extension holds no policy. It asks
which reservations to fetch, fetches them with the session the browser already
has, and posts back what Turo said. Which reservations are wanted, what to do
with a changed trip time, and whether a plate disagrees are all decided here,
where they are testable without a browser.

The payloads are Turo's, unmodified. Parsing them here rather than in the
extension means a change in Turo's shape is a change to one Python module with
tests, rather than to a TypeScript file that has to be rebuilt and side-loaded
before anyone can see whether it worked.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header
from pydantic import BaseModel
from sqlalchemy.orm import Session

from turonomics_api.db.base import get_session
from turonomics_api.ingest.reimbursements import relink_invoices
from turonomics_api.ingest.tolls import rematch_unattributed
from turonomics_api.ingest.turo_detail import (
    DetailResult,
    apply_detail,
    describe_grace_periods,
    parse_detail,
    read_grace,
    wanted_reservations,
)
from turonomics_api.ingest.turo_extras import ExtrasResult, apply_extras, wanted_extras
from turonomics_api.ingest.turo_invoice import (
    TuroInvoiceResult,
    apply_turo_invoice,
    hub_to_read,
    parse_hub,
    parse_turo_invoice,
    wanted_invoices,
)
from turonomics_api.ingest.turo_reservations import ReservationsResult, apply_reservations
from turonomics_api.routers.tolls import require_token, token_configured

log = logging.getLogger("turonomics.routers.turo")

router = APIRouter(prefix="/api/turo", tags=["turo"])

DbSession = Annotated[Session, Depends(get_session)]


# What Turo's own Trips page fetches (docs/design/03-turo-api.md). The query
# values were masked in the probe that found these routes, so they are the
# best reading of it and are reported on by every pull.
RESERVATION_LISTS: tuple[str, ...] = (
    "/api/v2/feeds/upcoming-trips?appMode=HOST",
    "/api/v2/feeds/trip-history?driverRoles=HOST&itemsPerPage={size}&page={page}",
)


class WantedResponse(BaseModel):
    """Which reservations the extension should fetch, and from where."""

    reservations: list[str]
    # The route, given once rather than hard-coded in the extension, so a
    # change to it does not need a side-loaded rebuild to fix.
    detail_path: str = "/api/reservation/detail?reservationId={id}&oppTermsAware=true"
    token_required: bool
    # Invoices whose breakdown the mail did not give, as [reservation, invoice]
    # pairs, and where Turo's invoice page reads one from.
    invoices: list[list[str]] = []
    invoice_path: str = "/api/v2/reservations/{id}/reimbursement/invoice/{invoice}"
    hub_path: str = "/api/reservations/{id}/invoice-hub"
    # Where Turo lists the account's reservations, so trips are found rather
    # than waited for in the mail. `{page}` and `{size}` are filled in by the
    # extension; a list without `{page}` is read once.
    reservation_lists: list[str] = list(RESERVATION_LISTS)
    # Trips whose photos and message thread are worth reading this pull, and
    # where Turo's reservation page reads each from.
    extras: list[str] = []
    photos_path: str = "/api/reservation/photos?reservationId={id}"
    messages_path: str = "/api/v2/reservation/conversation?reservationId={id}"


class DetailsIn(BaseModel):
    """Raw `/api/reservation/detail` bodies, exactly as Turo returned them."""

    details: list[dict[str, Any]]


class DetailsResponse(BaseModel):
    seen: int
    stored: int
    unparsed: int
    unknown: list[str]
    retimed: list[str]
    wrong_plate: list[str]
    # Crossings that found a rental once the times moved, and invoices that
    # could be reconciled as a result.
    tolls_rematched: int
    # Reservations whose invoice hub is worth reading, because Turo offers it.
    invoice_hubs: list[str] = []
    # One line per rental saying where Turo's gracePeriodEnd actually falls.
    # Here rather than in a log because the answer decides whether the toll
    # matcher can stop guessing, and a log line is easy to miss.
    grace_periods: list[str]


@router.get("/wanted", response_model=WantedResponse)
def wanted(session: DbSession) -> WantedResponse:
    return WantedResponse(
        reservations=wanted_reservations(session),
        token_required=token_configured(),
        invoices=[[reservation, invoice] for reservation, invoice in wanted_invoices(session)],
        extras=wanted_extras(session, now=datetime.now(UTC)),
    )


class GraceResponse(BaseModel):
    """Where Turo's ``gracePeriodEnd`` falls, per rental."""

    lines: list[str]
    # The reading, stated rather than left to the eye: a grace period a few
    # hours after the *start* is a cancellation deadline and no use for
    # attributing a late crossing; one after the *end* is the return grace the
    # toll matcher currently guesses at with a fixed two hours.
    verdict: str


@router.get("/grace", response_model=GraceResponse)
def grace(session: DbSession) -> GraceResponse:
    """A GET, because the answer is the point of having pulled it.

    The same report rides on the POST response, but that is gated behind the
    tolls token and arrives once, in a popup. This question — whether the
    matcher can stop guessing — is worth being able to ask again.
    """
    lines = describe_grace_periods(session)
    return GraceResponse(lines=lines, verdict=read_grace(lines))


@router.post("/details", response_model=DetailsResponse)
def post_details(
    payload: DetailsIn,
    session: DbSession,
    authorization: Annotated[str | None, Header()] = None,
) -> DetailsResponse:
    require_token(authorization)
    now = datetime.now(UTC)
    result = DetailResult()
    unparsed = 0
    hubs: list[str] = []
    for body in payload.details:
        detail = parse_detail(body)
        if detail is None:
            # Counted, not raised. A batch of forty where one came back as a
            # Turo error page should store the thirty-nine.
            unparsed += 1
            continue
        trip = apply_detail(session, detail, now=now, result=result)
        if trip is not None:
            trip.turo_detail = body
        if detail.has_invoices:
            hubs.append(detail.reservation_id)
    session.flush()

    # Only when something moved. Re-running attribution is cheap but it is not
    # free, and a pull that changed nothing should read as having changed
    # nothing.
    rematched = 0
    if result.retimed:
        rematched = rematch_unattributed(session)
        relink_invoices(session, now=now)
    session.commit()

    log.info(
        "turo pull: %d detail(s) — %d stored, %d unparsed, %d retimed, "
        "%d unknown reservation(s), %d crossing(s) rematched",
        result.seen,
        result.stored,
        unparsed,
        len(result.retimed),
        len(result.unknown),
        rematched,
    )
    for line in result.retimed:
        log.info("turo pull: booking moved — %s", line)
    for line in result.wrong_plate:
        log.warning("turo pull: plate disagrees — %s", line)

    return DetailsResponse(
        seen=result.seen,
        stored=result.stored,
        unparsed=unparsed,
        unknown=result.unknown,
        retimed=result.retimed,
        wrong_plate=result.wrong_plate,
        tolls_rematched=rematched,
        invoice_hubs=hubs,
        grace_periods=describe_grace_periods(session),
    )


class InvoiceIn(BaseModel):
    """One invoice-page body, with the reservation it was fetched for.

    The reservation travels beside the body because the body does not carry
    it: `tripInfo` has the times and the guest's first name, not the id.
    """

    reservation_id: str
    body: dict[str, Any]


class InvoicesIn(BaseModel):
    invoices: list[InvoiceIn]


class InvoicesResponse(BaseModel):
    seen: int
    unparsed: int
    matched: int
    created: int
    merged: int = 0
    # One line per invoice whose toll share is now known, saying what it was.
    itemised: list[str]
    tolls_asked: int
    tolls_recovered: int
    # Turo's reimbursementStatus values, reported because none has been read
    # unmasked yet and nothing here acts on them until one has.
    statuses: list[str]


@router.post("/invoices", response_model=InvoicesResponse)
def post_invoices(
    payload: InvoicesIn,
    session: DbSession,
    authorization: Annotated[str | None, Header()] = None,
) -> InvoicesResponse:
    require_token(authorization)
    now = datetime.now(UTC)
    result = TuroInvoiceResult()
    unparsed = 0
    for item in payload.invoices:
        invoice = parse_turo_invoice(item.reservation_id, item.body)
        if invoice is None:
            unparsed += 1
            continue
        row = apply_turo_invoice(session, invoice, now=now, result=result)
        row.turo_body = item.body
    session.commit()
    log.info(
        "turo invoices: %d read, %d unparsed, %d matched, %d new, %d merged, "
        "%d crossing(s) asked, %d recovered",
        result.seen,
        unparsed,
        result.matched,
        result.created,
        result.merged,
        result.tolls_asked,
        result.tolls_recovered,
    )
    for line in result.newly_itemised:
        log.info("turo invoices: %s", line)
    for line in result.statuses:
        log.info("turo invoices: status %s", line)
    return InvoicesResponse(
        seen=result.seen,
        unparsed=unparsed,
        matched=result.matched,
        created=result.created,
        merged=result.merged,
        itemised=result.newly_itemised,
        tolls_asked=result.tolls_asked,
        tolls_recovered=result.tolls_recovered,
        statuses=result.statuses,
    )


class HubIn(BaseModel):
    reservation_id: str
    body: dict[str, Any]


class HubsIn(BaseModel):
    hubs: list[HubIn]


class HubsResponse(BaseModel):
    seen: int
    unparsed: int
    # Every invoice the hubs listed, and the ones worth fetching, as
    # [reservation, invoice] pairs.
    listed: int
    to_read: list[list[str]]


@router.post("/hubs", response_model=HubsResponse)
def post_hubs(
    payload: HubsIn,
    session: DbSession,
    authorization: Annotated[str | None, Header()] = None,
) -> HubsResponse:
    """Which invoices to read, from the hubs that list them.

    A read, really, but behind the token like the other pull endpoints: it is
    only ever called by the pull, and it is sent Turo's bodies.
    """
    require_token(authorization)
    seen = unparsed = listed = 0
    to_read: list[list[str]] = []
    for item in payload.hubs:
        invoices = parse_hub(item.body)
        if invoices is None:
            unparsed += 1
            continue
        seen += 1
        listed += len(invoices)
        to_read.extend(
            [reservation, invoice]
            for reservation, invoice in hub_to_read(session, item.reservation_id, invoices)
        )
    log.info(
        "turo hubs: %d read, %d unparsed, %d invoice(s) listed, %d to read",
        seen,
        unparsed,
        listed,
        len(to_read),
    )
    return HubsResponse(seen=seen, unparsed=unparsed, listed=listed, to_read=to_read)


class ReservationsIn(BaseModel):
    """One page of a Turo reservation list, exactly as Turo returned it."""

    body: dict[str, Any]


class ReservationsResponse(BaseModel):
    found: int
    known: int
    created: list[str]
    unmatched: list[str]
    unreadable: int
    # The reservation ids on this page, in order, so the extension can tell a
    # page it has already seen — the end of the list, whichever way Turo
    # counts its pages — from a new one.
    ids: list[str]
    # Turo's page count, where the body states one.
    num_pages: int | None


def _num_pages(body: object, depth: int = 0) -> int | None:
    if depth > 3 or not isinstance(body, dict):
        return None
    found = body.get("numPages")
    if isinstance(found, int) and not isinstance(found, bool):
        return found
    for value in body.values():
        nested = _num_pages(value, depth + 1)
        if nested is not None:
            return nested
    return None


@router.post("/reservations", response_model=ReservationsResponse)
def post_reservations(
    payload: ReservationsIn,
    session: DbSession,
    authorization: Annotated[str | None, Header()] = None,
) -> ReservationsResponse:
    """Add any reservation on this page that the database does not have.

    The ones it does have are left to `POST /details`, which the extension
    calls next and which holds more than any list does.
    """
    require_token(authorization)
    result = ReservationsResult()
    ids = apply_reservations(session, payload.body, now=datetime.now(UTC), result=result)
    session.commit()
    if result.created or result.unmatched:
        log.info(
            "turo list: %d reservation(s), %d new, %d on a car not in the fleet",
            result.found,
            len(result.created),
            len(result.unmatched),
        )
    for line in result.unmatched:
        log.warning("turo list: no fleet car for reservation %s", line)
    return ReservationsResponse(
        found=result.found,
        known=result.known,
        created=result.created,
        unmatched=result.unmatched,
        unreadable=result.unreadable,
        ids=ids,
        num_pages=_num_pages(payload.body),
    )


class ExtrasItem(BaseModel):
    reservation_id: str
    # Turo's bodies as returned; None when the fetch failed, which is not the
    # same as a trip with no photos and is not stored as one.
    photos: Any = None
    messages: Any = None


class ExtrasIn(BaseModel):
    items: list[ExtrasItem]


class ExtrasResponse(BaseModel):
    stored: int
    unknown: list[str]


@router.post("/extras", response_model=ExtrasResponse)
def post_extras(
    payload: ExtrasIn,
    session: DbSession,
    authorization: Annotated[str | None, Header()] = None,
) -> ExtrasResponse:
    """Keep a batch of trips' photos and message threads for the trip view."""
    require_token(authorization)
    now = datetime.now(UTC)
    result = ExtrasResult()
    for item in payload.items:
        apply_extras(
            session,
            item.reservation_id,
            photos=item.photos,
            messages=item.messages,
            now=now,
            result=result,
        )
    session.commit()
    if result.stored:
        log.info("turo extras: photos and messages for %d trip(s)", result.stored)
    return ExtrasResponse(stored=result.stored, unknown=result.unknown)
