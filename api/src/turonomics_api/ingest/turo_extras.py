"""A trip's photos and message thread, from Turo's reservation page.

The operator's probe of a past trip's page showed two calls beside the detail
the pull already reads:

    GET /api/reservation/photos?reservationId=<id>
      images: [{imageId, uuid, step: TRIP_PHOTO | RENTER_CHECK_IN |
                RENTER_CHECK_OUT | OWNER_CHECK_IN | ..., photographerDriverRole,
                takenAtTime, createdTime, createdByDriver}]
    GET /api/v2/reservation/conversation?reservationId=<id>
      [{author: {firstName}, authorDriverRole: HOST | GUEST, sentTime, text,
        media: {images: [...]}}]

Both are kept whole on the trip for its view. Neither carries an image URL —
the photos are ids — so the view counts and times them rather than showing
them until the URL pattern is known.

Read once a trip has started, and again until a few days after it ends:
photos and messages keep arriving through the return and the inspection, and
after that a trip's thread is history.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from turonomics_api.db.models import Trip, TripState

# How long after a trip ends its photos and thread are still worth re-reading:
# the guest's return photos, the host's inspection, a dispute about either.
SETTLES_AFTER = timedelta(days=5)

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def wanted_extras(session: Session, *, now: datetime, limit: int = 40) -> list[str]:
    """Reservations whose photos and messages are worth reading this pull.

    Started, not cancelled, and either never read or read before the trip had
    settled. Never-read first, so a long history is worked through over a few
    pulls rather than the newest few being refreshed every time.
    """
    rows = session.scalars(
        select(Trip.turo_trip_id)
        .where(
            Trip.turo_trip_id.is_not(None),
            Trip.state != TripState.cancelled,
            Trip.starts_at <= now,
            or_(
                Trip.extras_synced_at.is_(None),
                Trip.extras_synced_at < Trip.ends_at + SETTLES_AFTER,
            ),
        )
        .order_by(Trip.extras_synced_at.is_not(None), Trip.ends_at.desc())
        .limit(limit)
    ).all()
    return [row for row in rows if row]


@dataclass
class ExtrasResult:
    stored: int = 0
    unknown: list[str] = field(default_factory=list)


def apply_extras(
    session: Session,
    reservation_id: str,
    *,
    photos: Any,
    messages: Any,
    now: datetime,
    result: ExtrasResult,
) -> None:
    """Keep what Turo returned. A body that is not the expected list is not
    stored over one that was: a failed fetch is not "no photos"."""
    trip = session.scalar(select(Trip).where(Trip.turo_trip_id == reservation_id))
    if trip is None:
        result.unknown.append(reservation_id)
        return
    images = photos.get("images") if isinstance(photos, Mapping) else None
    if isinstance(images, list):
        trip.turo_photos = images
    if isinstance(messages, list):
        trip.turo_messages = messages
    if isinstance(images, list) or isinstance(messages, list):
        trip.extras_synced_at = now
        result.stored += 1


def _moment(value: Any) -> datetime | None:
    if not isinstance(value, Mapping):
        return None
    millis = value.get("epochMillis")
    if not isinstance(millis, int) or isinstance(millis, bool):
        return None
    return _EPOCH + timedelta(milliseconds=millis)


@dataclass(frozen=True)
class PhotoGroup:
    step: str
    count: int
    by: str | None
    first: datetime | None
    last: datetime | None


# The order a trip happens in, so the view reads like the rental.
_STEP_ORDER = (
    "OWNER_CHECK_IN", "RENTER_CHECK_IN", "TRIP_PHOTO", "RENTER_CHECK_OUT", "OWNER_CHECK_OUT",
)


def photo_groups(photos: list[object] | None) -> list[PhotoGroup]:
    """A trip's photos counted by step, with who took them and when."""
    groups: dict[str, list[Mapping[str, Any]]] = {}
    for photo in photos or []:
        if isinstance(photo, Mapping):
            step = photo.get("step")
            groups.setdefault(step if isinstance(step, str) else "OTHER", []).append(photo)
    out: list[PhotoGroup] = []
    for step in sorted(
        groups, key=lambda s: _STEP_ORDER.index(s) if s in _STEP_ORDER else len(_STEP_ORDER)
    ):
        items = groups[step]
        times = sorted(t for t in (_moment(p.get("takenAtTime")) for p in items) if t)
        roles = Counter(
            p.get("photographerDriverRole")
            for p in items
            if isinstance(p.get("photographerDriverRole"), str)
        )
        out.append(
            PhotoGroup(
                step=step,
                count=len(items),
                by=str(roles.most_common(1)[0][0]) if roles else None,
                first=times[0] if times else None,
                last=times[-1] if times else None,
            )
        )
    return out


@dataclass(frozen=True)
class Message:
    role: str | None
    name: str | None
    sent_at: datetime | None
    text: str | None
    images: int


def thread(messages: list[object] | None) -> list[Message]:
    """The trip's messages, oldest first, as a person reads a conversation."""
    out: list[Message] = []
    for message in messages or []:
        if not isinstance(message, Mapping):
            continue
        author = message.get("author")
        author = author if isinstance(author, Mapping) else {}
        media = message.get("media")
        images = media.get("images") if isinstance(media, Mapping) else None
        role = message.get("authorDriverRole")
        name = author.get("firstName")
        text = message.get("text")
        out.append(
            Message(
                role=role if isinstance(role, str) else None,
                name=name if isinstance(name, str) else None,
                sent_at=_moment(message.get("sentTime")),
                text=text if isinstance(text, str) else None,
                images=len(images) if isinstance(images, list) else 0,
            )
        )
    out.sort(key=lambda m: m.sent_at or _EPOCH)
    return out
