"""Read Turo mail on the poll and turn it into trips.

Runs alongside the Bouncie sync rather than on its own schedule: a trip and the
car's position are read in the same cycle, so the run sheet never shows a car
as free while the mail that says otherwise waits for a different timer.

Only recent mail is scanned. Turo sends a notification for every state change,
so a trip's current state is always in the last few days of mail — there is no
need to re-read a year of it on every poll, and re-reading it would spend the
Gmail quota for nothing.
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from turonomics_api.db.models import Trip, Vehicle
from turonomics_api.gmail.client import GmailClient, GmailError
from turonomics_api.gmail.parse import TRIP_BEARING, ParseError, classify, parse_email
from turonomics_api.gmail.probe import (
    SECONDS_BETWEEN_FETCHES,
    html_of,
    plain_text,
    shape_of,
)
from turonomics_api.ingest.tasks import refresh_move_task
from turonomics_api.ingest.trips import TripSyncResult, apply_parsed_trip
from turonomics_api.ingest.turnaround import refresh_turnaround_tasks
from turonomics_api.settings import fleet_timezone

log = logging.getLogger("turonomics.ingest.mail")

DEFAULT_QUERY = "from:turo newer_than:7d"
DEFAULT_LIMIT = 40


def _header(payload: dict[str, Any], name: str) -> str:
    for item in payload.get("headers") or []:
        if item.get("name", "").lower() == name.lower():
            return str(item.get("value", ""))
    return ""


def _received_at(payload: dict[str, Any], fallback: datetime) -> datetime:
    """When Gmail says the message arrived, used to resolve years on labels."""
    raw = payload.get("internalDate")
    if raw:
        try:
            return datetime.fromtimestamp(int(raw) / 1000, tz=UTC)
        except (ValueError, OSError):
            pass
    return fallback


def sync_trips_from_mail(
    session: Session,
    *,
    client: GmailClient | None = None,
    query: str | None = None,
    limit: int | None = None,
    now: datetime | None = None,
) -> TripSyncResult:
    """Scan recent Turo mail and upsert the trips it describes. Never raises."""
    now = now or datetime.now(UTC)
    result = TripSyncResult()
    query = query or os.environ.get("TURO_MAIL_QUERY", "").strip() or DEFAULT_QUERY
    limit = limit or DEFAULT_LIMIT

    try:
        gmail = client or GmailClient(session)
        ids = gmail.search(query, limit=limit)
    except GmailError as exc:
        # Not connected, or revoked. The fleet view still works off Bouncie, so
        # this is a degraded poll rather than a failed one.
        log.warning("skipping mail sync: %s", exc)
        return result

    touched: set[uuid.UUID] = set()
    scanned = 0
    not_a_trip = 0
    unreadable = 0
    first_skipped: str | None = None
    first_shape: object | None = None
    for index, message_id in enumerate(ids):
        if index:
            time.sleep(SECONDS_BETWEEN_FETCHES)
        try:
            message = gmail.message(message_id)
        except GmailError as exc:
            log.warning("skipping a message: %s", exc)
            unreadable += 1
            continue
        scanned += 1

        payload = message.get("payload") or {}
        try:
            parsed = parse_email(
                subject=_header(payload, "Subject"),
                body=plain_text(payload),
                received_at=_received_at(message, now),
                fleet_timezone=str(fleet_timezone()),
                # The markup as well as the text: plain_text() de-tags it, and
                # the href behind the car's photo is the only thing in a Turo
                # email that identifies the car rather than describing it.
                html=html_of(payload),
            )
        except ParseError as exc:
            # Most Turo mail genuinely is not a trip — payouts, marketing,
            # inspections — so this is not per-message worthy. But the *reason*
            # for the first one is, because "every message failed to parse" and
            # "there was no trip mail this week" produce identical counts
            # otherwise, and that ambiguity cost a deploy to notice.
            not_a_trip += 1
            if first_skipped is None:
                kind = classify(_header(payload, "Subject"))
                first_skipped = f"{kind}: {exc}"
                # Keep the masked shape of the first trip-classified message
                # that would not parse. The first round of this guessed at why
                # dates were missing and guessed wrong; the shape is the
                # evidence, and masking is what makes it safe to log.
                if kind in TRIP_BEARING:
                    first_shape = shape_of(message)
            continue

        before = session.scalar(
            select(Trip.id).where(Trip.turo_trip_id == parsed.reservation_id)
        )
        trip = apply_parsed_trip(session, parsed, now=now)
        if trip is None:
            result.unmatched += 1
            continue
        if before is None:
            result.created += 1
        else:
            result.updated += 1
        touched.add(trip.vehicle_id)

    # A new or cancelled trip changes whether a street-cleaning alert applies
    # and whether there is prep to do, so the affected vehicles' tasks are
    # refreshed in the same cycle.
    for vehicle_id in touched:
        vehicle = session.get(Vehicle, vehicle_id)
        if vehicle is not None:
            refresh_move_task(session, vehicle=vehicle, now=now)
            refresh_turnaround_tasks(session, vehicle=vehicle, now=now)

    # Logged unconditionally. A sync that reports nothing when it found
    # nothing is indistinguishable from one that did not run, and the first
    # live run of this module was exactly that: silence, with no way to tell
    # whether the mailbox was empty of trips or the parser was rejecting all of
    # them.
    log.info(
        "mail sync: %d message(s) for %r — %s; %d not a trip, %d unreadable",
        scanned,
        query,
        result.summary(),
        not_a_trip,
        unreadable,
    )
    if first_skipped and not (result.created or result.updated):
        # Nothing landed, so the first rejection is the most useful clue there
        # is. Only the classification and the reason — no subject text.
        log.info("mail sync: nothing parsed; first skip was %s", first_skipped)
        if first_shape is not None:
            # Values are type tokens, same as the probe. A shape in the log is
            # what turns the next fix from a guess into a correction.
            log.info("mail sync: shape of that message —")
            for label in getattr(first_shape, "labels", []):
                log.info("    label : %s", label)
            for line in getattr(first_shape, "lines", []):
                log.info("    line  : %s", line)
            for link in getattr(first_shape, "links", []):
                log.info("    link  : %s", link)
    return result
