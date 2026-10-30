"""Turo's listing and account settings, read and changed through the extension.

Turo answers these calls only from the operator's own logged-in browser, so
each read or change is an extension command naming an operation. The
extension holds the list of operations and how each is called. This module
only names one, which keeps the API, or anyone who reaches it, from making
the browser call an arbitrary Turo address.

For a change the extension reads the setting, writes it, reads it back, and
reports before and after with the values that would put it back. That report
is kept on the command, so the commands table is the log of every change, and
undoing one replays its restore values as a new change.

What is here is what the operator chose to allow directly: settings a change
to which can be reversed, and calendar prices, set on dates no reservation
touches and within Turo's own bounds (the extension checks both against the
live calendar). Blocked days, and anything that cannot be taken back, are
not.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from turonomics_api.db.models import ExtensionCheckin, ExtensionCommand, Vehicle
from turonomics_api.routers.commands import (
    DONE,
    FAILED,
    LISTENING_WITHIN,
    QUEUED,
    TURO,
    TURO_MIN_VERSION,
    version_at_least,
)

CAR, ACCOUNT = "car", "account"


@dataclass(frozen=True)
class Setting:
    scope: str
    about: str
    # False for an add, an edit or a removal of one item: those take an id or
    # make one, and read through the setting they belong to.
    readable: bool = True


# Mirrors the extension's list (extension/src/turo-settings.ts). A name here
# the extension does not know is refused by the extension, not run.
SETTINGS: dict[str, Setting] = {
    "guest_instructions": Setting(CAR, (
        "Welcome message, pickup and return instructions, car guide. values: any of "
        "welcomeMessage, pickupReturnInstructions, carGuide (strings); the rest are kept.")),
    "listing_text": Setting(CAR, (
        "The listing's description and name. values: description and/or vehicleName.")),
    "badges": Setting(CAR, (
        "The listing's badges. values: badgeIds, the whole set (ints); read lists the "
        "options.")),
    "photo_order": Setting(CAR, (
        "Photo order. values: imageIds, every photo's id in the new order.")),
    "trip_preferences": Setting(CAR, (
        "Buffer between trips, lead time, minimum and maximum trip length, which "
        "pickup locations are on. values: any of the fields read shows, each set to one "
        "of its option names (the enum name, not the display text).")),
    "distance_limit": Setting(CAR, (
        "Daily and monthly distance limits. values: dailyDistanceLimit "
        "{scalar, unit, unlimited}, monthlyDistanceEnabled, monthlyDistanceLimit; read "
        "lists the allowed options.")),
    "extras": Setting(CAR, "The car's extras and their prices. Read only; see extra_*."),
    "extra_add": Setting(CAR, (
        "Add an extra. values: extraType (from read extras' types), description, price "
        "(whole dollars), quantity, extraPricingType (PER_TRIP or PER_DAY), enabled."),
        readable=False),
    "extra_update": Setting(CAR, (
        "Change an extra. values: extraId and any of description, price, quantity, "
        "extraPricingType, enabled."), readable=False),
    "extra_remove": Setting(CAR, "Remove an extra. values: extraId.", readable=False),
    "faqs": Setting(CAR, "The listing's questions and answers. Read only; see faq_*."),
    "faq_add": Setting(CAR, "Add a question. values: question, answer.", readable=False),
    "faq_update": Setting(CAR, "Change one. values: faqId, question, answer.", readable=False),
    "faq_remove": Setting(CAR, "Remove one. values: faqId.", readable=False),
    "features": Setting(CAR, "The listing's extra feature lines. Read only; see feature_*."),
    "feature_add": Setting(CAR, "Add a feature line. values: feature.", readable=False),
    "feature_update": Setting(CAR, "Change one. values: featureId, feature.", readable=False),
    "feature_remove": Setting(CAR, "Remove one. values: featureId.", readable=False),
    "price_calendar": Setting(CAR, (
        "The car's calendar, a day per row: price and source (OWNER, the host's; DYNAMIC, "
        "Turo's automatic), whether it is booked, blocked or editable, Turo's min and max, "
        "the price Turo calls high for the day (overpriced), and Turo's automatic price "
        "(automatic, next 90 days). Read only. values: startDate (YYYY-MM-DD, default "
        "today), days (default 60, at most 120).")),
    "market": Setting(CAR, (
        "Comparable cars near this one by month: median, weekday and weekend prices, how "
        "many there are and how booked (utilization_percent). Read only.")),
    "prices": Setting(CAR, (
        "Set calendar prices. values: prices, a list of {date: YYYY-MM-DD, amount: whole "
        "dollars}, and/or automatic, a list of dates to give back to Turo's automatic "
        "price. Refused whole, naming the dates, if any is booked, past, over a year out, "
        "or outside Turo's min and max; each date's outcome is reported."),
        readable=False),
    "business_hours": Setting(ACCOUNT, (
        "When guests can pick up and return. values: alwaysAvailable and/or week, the "
        "whole week as read shows it.")),
    "notifications": Setting(ACCOUNT, (
        "Which Turo notifications arrive and how. values: preferences, a list of "
        "{communication_theme, delivery_channel, value}; only those listed change.")),
    "sms": Setting(ACCOUNT, "Whether Turo texts the host. values: sms (true or false)."),
}

# How long a tool call waits for the extension before answering "queued".
WAIT_SECONDS = 25.0


class ChangeError(ValueError):
    """Said to the caller as the reason, not raised as a fault."""


def _vehicle_id(session: Session, car: str | None) -> str:
    if not car:
        raise ChangeError("which car? give its nickname")
    wanted = car.strip().lower()
    for vehicle in session.scalars(select(Vehicle)):
        names = {(vehicle.nickname or "").lower(), (vehicle.bouncie_nickname or "").lower()}
        if wanted in names:
            if not vehicle.turo_listing_id:
                raise ChangeError(f"{vehicle.nickname} has no Turo listing id recorded")
            return vehicle.turo_listing_id
    raise ChangeError(f"no car called {car}")


def _extension_ready(session: Session) -> str | None:
    """Why the extension cannot run this, or None if it can."""
    checkin = session.get(ExtensionCheckin, 1)
    if checkin is None:
        return "no browser extension has checked in"
    if not version_at_least(checkin.version, TURO_MIN_VERSION):
        wanted = ".".join(str(p) for p in TURO_MIN_VERSION)
        return f"Turo settings need extension {wanted} or later; the one checking in is " \
               f"{checkin.version or 'unknown'}"
    return None


def queue(
    session: Session, setting: str, mode: str, car: str | None, values: dict[str, Any]
) -> ExtensionCommand:
    found = SETTINGS.get(setting)
    if found is None:
        raise ChangeError(f"setting is one of {', '.join(SETTINGS)}")
    if mode == "read" and not found.readable:
        raise ChangeError(f"{setting} is a change; read the setting it belongs to")
    if not isinstance(values, dict):
        raise ChangeError("values is an object")
    if mode == "write" and not values:
        raise ChangeError("values is empty: nothing to change")
    problem = _extension_ready(session)
    if problem is not None:
        raise ChangeError(problem)
    payload: dict[str, Any] = {"operation": setting, "mode": mode, "values": values}
    if found.scope == CAR:
        payload["vehicle_id"] = _vehicle_id(session, car)
    command = ExtensionCommand(kind=TURO, state=QUEUED, requested_at=datetime.now(UTC),
                               payload=payload)
    session.add(command)
    session.commit()
    return command


def wait(session: Session, command: ExtensionCommand) -> None:
    """Until the extension has answered, or the time is up. Only while it is listening."""
    checkin = session.get(ExtensionCheckin, 1)
    if checkin is None or datetime.now(UTC) - checkin.seen_at > LISTENING_WITHIN:
        return
    deadline = time.monotonic() + WAIT_SECONDS
    while time.monotonic() < deadline:
        session.expire_all()
        if command.state in (DONE, FAILED):
            return
        time.sleep(0.5)
    session.expire_all()


def view(command: ExtensionCommand) -> dict[str, Any]:
    payload = command.payload or {}
    detail = command.detail or {}
    out: dict[str, Any] = {
        "change_id": str(command.id),
        "setting": payload.get("operation"),
        "mode": payload.get("mode"),
        "values": payload.get("values"),
        "state": command.state,
        "result": command.result,
        "requested_at": command.requested_at.isoformat(),
        "finished_at": command.finished_at.isoformat() if command.finished_at else None,
    }
    for key in ("before", "after", "current", "options"):
        if key in detail:
            out[key] = detail[key]
    # A change reported failed can be partly made (some dates priced, not
    # others), so it can be undone too: the restore covers everything asked.
    out["can_undo"] = bool(detail.get("restore")) and command.state in (DONE, FAILED)
    if command.state == QUEUED:
        out["next"] = ("The extension has not picked it up yet; turo_changes shows it "
                       "when it does.")
    return out


def undo(session: Session, change_id: uuid.UUID) -> ExtensionCommand:
    command = session.get(ExtensionCommand, change_id)
    if command is None or command.kind != TURO:
        raise ChangeError("no Turo change with that id")
    restore = (command.detail or {}).get("restore")
    if command.state not in (DONE, FAILED) or not restore:
        raise ChangeError("that change cannot be undone: it is still running, or it was a read")
    payload = command.payload or {}
    vehicle = payload.get("vehicle_id")
    values = restore.get("values") or {}
    found = SETTINGS.get(str(restore.get("operation")))
    if found is None:
        raise ChangeError("the extension reported a restore this API does not know")
    problem = _extension_ready(session)
    if problem is not None:
        raise ChangeError(problem)
    again: dict[str, Any] = {"operation": restore["operation"], "mode": "write",
                             "values": values, "undoes": str(command.id)}
    if vehicle:
        again["vehicle_id"] = vehicle
    redo = ExtensionCommand(kind=TURO, state=QUEUED, requested_at=datetime.now(UTC),
                            payload=again)
    session.add(redo)
    session.commit()
    return redo


def recent(session: Session, limit: int) -> list[ExtensionCommand]:
    return list(session.scalars(
        select(ExtensionCommand).where(ExtensionCommand.kind == TURO)
        .order_by(ExtensionCommand.requested_at.desc()).limit(limit)))
