"""SQLAlchemy models.

Design notes that are load-bearing, not incidental:

* ``TelemetryEvent`` is append-only. Derived state (where a car is parked, what
  the tank reads) is computed from it, never edited in place, so the derivation
  can be changed and replayed. Note Bouncie has no ignition-on/off event: the
  parked position derives from ``tripEnd``, not from an ignition signal.
* Bouncie's ``stats.localTimeZone`` is a UTC offset string ("-0400"), not an
  IANA zone, so it cannot describe a DST transition. Street-cleaning deadlines
  are computed in ``FLEET_TIMEZONE`` and never from that field.
* ``Task`` is the one abstraction the run sheet consumes. Every module emits
  Tasks; nothing else reaches the run sheet. Cross-module rules — suppressing a
  street-cleaning alert for a car out on a guest trip — are then a single rule
  over Tasks rather than four modules aware of each other.
* ``Task.owner_id`` is nullable from day one and nothing assumes a current
  user, so assignment becomes a feature rather than a migration when a helper
  starts (decision D7).
* Wall-clock times for street cleaning are stored as naive ``time`` and
  interpreted in ``FLEET_TIMEZONE``. Everything else is ``timestamptz``. Street
  cleaning is a local-time rule and DST shifts it, so a UTC-only model computes
  every deadline an hour wrong for half the year.
"""

from __future__ import annotations

import enum
import uuid
from datetime import date, datetime, time

from geoalchemy2 import Geography
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    Time,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, validates

from turonomics_api.plates import normalize_plate


class Base(DeclarativeBase):
    pass


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(primary_key=True, default=uuid.uuid4)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class StreetSide(enum.StrEnum):
    north = "north"
    south = "south"
    east = "east"
    west = "west"


class RuleSource(enum.StrEnum):
    nyc_signs = "nyc_signs"  # parsed from the Open Data sign dataset
    captured = "captured"  # read off the sign by the operator (D4)
    manual = "manual"  # entered or corrected by hand (D3 override)


class TaskKind(enum.StrEnum):
    asp_move = "asp_move"
    turnaround = "turnaround"
    trip_start = "trip_start"
    trip_end = "trip_end"
    message = "message"
    maintenance = "maintenance"
    fuel = "fuel"


class TaskState(enum.StrEnum):
    open = "open"
    done = "done"
    suppressed = "suppressed"
    cancelled = "cancelled"


class TripState(enum.StrEnum):
    upcoming = "upcoming"
    active = "active"
    completed = "completed"
    cancelled = "cancelled"


class TripSource(enum.StrEnum):
    extension = "extension"
    email = "email"
    manual = "manual"


# ---------------------------------------------------------------------------
# People
# ---------------------------------------------------------------------------


class User(Base):
    """An operator. One today; the model does not assume that."""

    __tablename__ = "app_user"

    id: Mapped[uuid.UUID] = _uuid_pk()
    email: Mapped[str] = mapped_column(String(320), unique=True)
    google_sub: Mapped[str | None] = mapped_column(String(255), unique=True)
    display_name: Mapped[str | None] = mapped_column(String(200))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# ---------------------------------------------------------------------------
# Provider credentials
# ---------------------------------------------------------------------------


class OAuthToken(Base):
    """Stored provider tokens.

    Bouncie rotates refresh tokens: each refresh returns a new one and
    invalidates the old, and an unused refresh token eventually expires. So the
    new pair is persisted before the access token is used, and a broken chain
    recovers by re-exchanging the authorization code — which, unusually, never
    expires.

    Tokens are stored as plaintext. Acceptable for a single-operator deployment
    on managed Postgres; worth revisiting before anyone else has database
    access.
    """

    __tablename__ = "oauth_token"

    id: Mapped[uuid.UUID] = _uuid_pk()
    provider: Mapped[str] = mapped_column(String(40), unique=True)

    access_token: Mapped[str] = mapped_column(Text)
    refresh_token: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    obtained_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    refresh_count: Mapped[int] = mapped_column(Integer, default=0)


# ---------------------------------------------------------------------------
# Fleet
# ---------------------------------------------------------------------------


class Vehicle(Base):
    __tablename__ = "vehicle"

    id: Mapped[uuid.UUID] = _uuid_pk()

    nickname: Mapped[str] = mapped_column(String(60), unique=True)
    make: Mapped[str] = mapped_column(String(60))
    model: Mapped[str] = mapped_column(String(60))
    year: Mapped[int] = mapped_column(SmallInteger)

    # Plate joins to the EZPass matcher, VIN joins to telemetry. Normalised on
    # write by the same rule the toll parser uses.
    #
    # Nullable on purpose: Bouncie does not know plates, so a vehicle seeded
    # from a device has none until someone enters it. A placeholder string
    # would be a plate-shaped value that silently matches nothing, which reads
    # as "no tolls this month" rather than as missing data. Postgres permits
    # several NULLs under a unique index, so more than one vehicle may be
    # awaiting a plate.
    plate: Mapped[str | None] = mapped_column(String(16), unique=True, index=True)
    vin: Mapped[str | None] = mapped_column(String(17), unique=True)

    bouncie_imei: Mapped[str | None] = mapped_column(String(32), unique=True, index=True)
    turo_listing_id: Mapped[str | None] = mapped_column(String(40))

    # What Bouncie calls this vehicle, so device-sourced rows can be matched
    # back to a registry row by something a human recognises.
    bouncie_nickname: Mapped[str | None] = mapped_column(String(60))

    tank_gallons: Mapped[float | None] = mapped_column(Float)

    # Bouncie reports fuel level only where the vehicle sends it over OBD, and
    # odometer has three tiers of fidelity. Discovered at setup; check-out falls
    # back to manual entry per car where these are false.
    reports_fuel_level: Mapped[bool | None] = mapped_column(Boolean)
    reports_obd_odometer: Mapped[bool | None] = mapped_column(Boolean)

    # The Transit runs passenger plates, so no commercial parking rules apply
    # (D11) — but it does not physically fit everywhere a Corolla does, and NYC
    # has no length-based cleaning rule. Parking *suggestions* filter on this.
    needs_large_spot: Mapped[bool] = mapped_column(Boolean, default=False)

    registration_expires: Mapped[date | None] = mapped_column(Date)
    inspection_expires: Mapped[date | None] = mapped_column(Date)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    @validates("plate")
    def _normalize_plate(self, _key: str, value: str | None) -> str | None:
        # Same rule the toll matcher applies, so the join cannot miss because
        # someone typed "LEH-9892" here and the CSV said "LEH9892".
        if value is None:
            return None
        return normalize_plate(value) or None

    telemetry: Mapped[list[TelemetryEvent]] = relationship(back_populates="vehicle")
    parking_sessions: Mapped[list[ParkingSession]] = relationship(back_populates="vehicle")
    trips: Mapped[list[Trip]] = relationship(back_populates="vehicle")
    tasks: Mapped[list[Task]] = relationship(back_populates="vehicle")


class TelemetryEvent(Base):
    """Raw Bouncie events, append-only.

    Nothing updates a row here. Parked position, fuel and odometer are derived
    from this stream, so a change to the derivation logic is a replay rather
    than a data migration.
    """

    __tablename__ = "telemetry_event"

    id: Mapped[uuid.UUID] = _uuid_pk()
    vehicle_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("vehicle.id", ondelete="CASCADE"))

    event_type: Mapped[str] = mapped_column(String(50))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    location = mapped_column(Geography("POINT", srid=4326, spatial_index=False), nullable=True)
    heading_deg: Mapped[float | None] = mapped_column(Float)
    speed_mph: Mapped[float | None] = mapped_column(Float)

    # Bouncie reports engine state directly, so "parked" does not have to be
    # inferred from consecutive stationary fixes or waited for as a tripEnd
    # webhook. None means the provider did not say, which is not the same as
    # stopped.
    is_running: Mapped[bool | None] = mapped_column(Boolean)

    fuel_percent: Mapped[float | None] = mapped_column(Float)
    odometer_miles: Mapped[float | None] = mapped_column(Float)
    # Bouncie reports battery as a status string ("normal"), not a voltage,
    # despite what a generic OBD integration would lead you to expect.
    battery_status: Mapped[str | None] = mapped_column(String(30))
    mil_on: Mapped[bool | None] = mapped_column(Boolean)
    dtc_count: Mapped[int | None] = mapped_column(SmallInteger)

    # The provider's own payload, kept verbatim so a field we didn't model yet
    # is still recoverable without re-fetching.
    payload: Mapped[dict[str, object]] = mapped_column(JSONB, default=dict)

    # Provider event id where one exists, so webhook retries are idempotent.
    # Bouncie retries with backoff for up to ~11 hours, so duplicates are
    # expected rather than exceptional.
    provider_event_id: Mapped[str | None] = mapped_column(String(128))

    vehicle: Mapped[Vehicle] = relationship(back_populates="telemetry")

    __table_args__ = (
        UniqueConstraint("vehicle_id", "provider_event_id", name="uq_telemetry_provider_event"),
        Index("ix_telemetry_vehicle_time", "vehicle_id", "occurred_at"),
        Index("ix_telemetry_location", "location", postgresql_using="gist"),
    )


# ---------------------------------------------------------------------------
# Parking and alternate-side rules
# ---------------------------------------------------------------------------


class StreetSegmentSide(Base):
    """One side of one block. ASP rules are side-specific, so the side is part
    of the identity, not an attribute."""

    __tablename__ = "street_segment_side"

    id: Mapped[uuid.UUID] = _uuid_pk()

    street_name: Mapped[str] = mapped_column(String(120), index=True)
    from_cross_street: Mapped[str | None] = mapped_column(String(120))
    to_cross_street: Mapped[str | None] = mapped_column(String(120))
    borough: Mapped[str] = mapped_column(String(20), default="Brooklyn")
    side: Mapped[StreetSide] = mapped_column(Enum(StreetSide, name="street_side"))

    # The curb line, not the street centreline — a point snaps to the nearer of
    # the two sides, which is the whole difficulty.
    #
    # GEOMETRY rather than LINESTRING because the source is sign positions, and
    # signs sit on the curb: two or more on a block-side trace the curb, but 9%
    # of block-sides carry a single sign and a point is the honest
    # representation of those. Distance queries work against either.
    geom = mapped_column(Geography("GEOMETRY", srid=4326, spatial_index=False), nullable=True)

    # NYC has no length-based cleaning rule, so van fit is a spot property, not
    # a rule property. Parking suggestions for the Transit filter on it.
    fits_van: Mapped[bool | None] = mapped_column(Boolean)

    nyc_physical_id: Mapped[str | None] = mapped_column(String(40), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    rules: Mapped[list[AspRule]] = relationship(back_populates="segment_side")

    __table_args__ = (
        UniqueConstraint(
            "street_name",
            "from_cross_street",
            "to_cross_street",
            "borough",
            "side",
            name="uq_segment_side",
        ),
        Index("ix_segment_geom", "geom", postgresql_using="gist"),
    )


class AspRule(Base):
    """A cleaning window on one segment side.

    ``days_of_week`` is ISO: Monday=1 … Sunday=7. ``starts_at``/``ends_at`` are
    local wall-clock in FLEET_TIMEZONE, deliberately not UTC — the sign says
    11:30am year round, and UTC would drift it by an hour across DST.
    """

    __tablename__ = "asp_rule"

    id: Mapped[uuid.UUID] = _uuid_pk()
    segment_side_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("street_segment_side.id", ondelete="CASCADE")
    )

    days_of_week: Mapped[list[int]] = mapped_column(ARRAY(SmallInteger))
    starts_at: Mapped[time] = mapped_column(Time)
    ends_at: Mapped[time] = mapped_column(Time)

    source: Mapped[RuleSource] = mapped_column(Enum(RuleSource, name="rule_source"))
    # 1.0 for a rule the operator read off the sign; lower for a parsed one.
    # Below the alerting threshold the app asks for a capture instead of
    # guessing, and never stays silent (D4).
    confidence: Mapped[float] = mapped_column(Float, default=1.0)

    raw_sign_text: Mapped[str | None] = mapped_column(Text)
    effective_from: Mapped[date | None] = mapped_column(Date)
    effective_to: Mapped[date | None] = mapped_column(Date)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("app_user.id"))

    segment_side: Mapped[StreetSegmentSide] = relationship(back_populates="rules")

    __table_args__ = (
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="ck_asp_confidence"),
        CheckConstraint(
            # array_length('{}', 1) is NULL, not 0, and a CHECK that evaluates to
            # NULL passes -- so without the coalesce this admits a cleaning rule
            # with no days, which would silently never fire.
            "coalesce(array_length(days_of_week, 1), 0) > 0",
            name="ck_asp_days_present",
        ),
        Index("ix_asp_rule_segment", "segment_side_id"),
    )


class AspSuspension(Base):
    """Days when alternate-side is suspended citywide.

    Planned suspensions come from the DOT calendar .ics; emergency ones (snow)
    are announced same-day and arrive by a different path, hence ``source``.
    """

    __tablename__ = "asp_suspension"

    id: Mapped[uuid.UUID] = _uuid_pk()
    suspended_on: Mapped[date] = mapped_column(Date, unique=True, index=True)
    reason: Mapped[str | None] = mapped_column(String(200))
    source: Mapped[str] = mapped_column(String(40), default="dot_calendar")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ParkingSession(Base):
    """Where a vehicle is parked, and until when it may stay.

    ``segment_side_id`` stays null until the guess is confirmed. GPS cannot
    resolve which side of an 11 m street a car is on, so the app confirms in one
    tap rather than automating it blindly, and caches the answer for that spot.
    """

    __tablename__ = "parking_session"

    id: Mapped[uuid.UUID] = _uuid_pk()
    vehicle_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("vehicle.id", ondelete="CASCADE"))

    location = mapped_column(Geography("POINT", srid=4326, spatial_index=False), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    segment_side_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("street_segment_side.id", ondelete="SET NULL")
    )
    guessed_segment_side_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("street_segment_side.id", ondelete="SET NULL")
    )
    guess_confidence: Mapped[float | None] = mapped_column(Float)

    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    confirmed_by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("app_user.id"))
    # True when the operator corrected the guess — the signal worth learning from.
    was_corrected: Mapped[bool] = mapped_column(Boolean, default=False)

    # Cached so the run sheet doesn't recompute rules per render. Recomputed
    # when the rule, the suspension calendar or the confirmed side changes.
    must_move_by: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)

    vehicle: Mapped[Vehicle] = relationship(back_populates="parking_sessions")

    # Two foreign keys point at the same table, so each relationship has to say
    # which one it follows.
    segment_side: Mapped[StreetSegmentSide | None] = relationship(
        foreign_keys=[segment_side_id]
    )
    guessed_segment_side: Mapped[StreetSegmentSide | None] = relationship(
        foreign_keys=[guessed_segment_side_id]
    )

    __table_args__ = (
        Index("ix_parking_vehicle_active", "vehicle_id", "ended_at"),
        Index("ix_parking_location", "location", postgresql_using="gist"),
    )


# ---------------------------------------------------------------------------
# Trips
# ---------------------------------------------------------------------------


class Trip(Base):
    __tablename__ = "trip"

    id: Mapped[uuid.UUID] = _uuid_pk()
    vehicle_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("vehicle.id", ondelete="CASCADE"))

    turo_trip_id: Mapped[str | None] = mapped_column(String(40), unique=True, index=True)
    guest_name: Mapped[str | None] = mapped_column(String(200))

    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    state: Mapped[TripState] = mapped_column(Enum(TripState, name="trip_state"))

    # Which adapter produced this, and when it was last seen. The Turo adapter
    # is best-effort and replaceable, so staleness is surfaced honestly rather
    # than hidden behind a confidently wrong schedule.
    source: Mapped[TripSource] = mapped_column(Enum(TripSource, name="trip_source"))
    synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    earnings_cents: Mapped[int | None] = mapped_column(Integer)

    vehicle: Mapped[Vehicle] = relationship(back_populates="trips")

    __table_args__ = (
        CheckConstraint("ends_at > starts_at", name="ck_trip_interval"),
        Index("ix_trip_vehicle_window", "vehicle_id", "starts_at", "ends_at"),
    )


# ---------------------------------------------------------------------------
# The unifying abstraction
# ---------------------------------------------------------------------------


class Task(Base):
    """Every open obligation, whatever produced it.

    Modules produce Tasks; the run sheet consumes nothing else. That is what
    lets "this car is out on a guest trip, so don't nag about street cleaning"
    be one rule over Tasks instead of four modules knowing about each other —
    see ``suppressed_reason``.
    """

    __tablename__ = "task"

    id: Mapped[uuid.UUID] = _uuid_pk()
    vehicle_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("vehicle.id", ondelete="CASCADE"))

    kind: Mapped[TaskKind] = mapped_column(Enum(TaskKind, name="task_kind"))
    state: Mapped[TaskState] = mapped_column(
        Enum(TaskState, name="task_state"), default=TaskState.open, index=True
    )

    title: Mapped[str] = mapped_column(String(200))
    detail: Mapped[str | None] = mapped_column(Text)

    due_by: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    location = mapped_column(Geography("POINT", srid=4326, spatial_index=False), nullable=True)
    location_label: Mapped[str | None] = mapped_column(String(200))

    # Nullable, and nothing assumes a current user (D7). Assignment becomes a
    # feature rather than a migration when a helper starts.
    owner_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("app_user.id", ondelete="SET NULL")
    )

    suppressed_reason: Mapped[str | None] = mapped_column(String(200))

    # What produced this task, so a regenerating module can find its own rows
    # instead of duplicating them.
    source_kind: Mapped[str | None] = mapped_column(String(40))
    source_id: Mapped[uuid.UUID | None] = mapped_column()

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("app_user.id"))

    vehicle: Mapped[Vehicle] = relationship(back_populates="tasks")

    __table_args__ = (
        UniqueConstraint("source_kind", "source_id", "kind", name="uq_task_source"),
        Index("ix_task_open_due", "state", "due_by"),
        Index("ix_task_location", "location", postgresql_using="gist"),
    )
