from datetime import datetime

from pydantic import BaseModel, field_validator  # field_validator used by EZPassToll / TuroTrip

# Aliased: the validators below share these names, and the shared rule is the
# one the registry also applies, so a hand-typed plate matches a parsed one.
from turonomics_api.plates import normalize_optional_plate as _normalize_optional
from turonomics_api.plates import normalize_plate as _normalize


class TuroTrip(BaseModel):
    trip_id: str
    start: datetime
    end: datetime
    license_plate: str

    @field_validator("license_plate")
    @classmethod
    def normalize_plate(cls, v: str) -> str:
        return _normalize(v)


class EZPassToll(BaseModel):
    timestamp: datetime
    plaza: str
    amount: float
    transponder_id: str | None = None  # None when tag/plate field contains a license plate
    license_plate: str | None = None   # None when tag/plate field contains a transponder

    @field_validator("license_plate")
    @classmethod
    def normalize_plate(cls, v: str | None) -> str | None:
        return _normalize_optional(v)

    @field_validator("transponder_id")
    @classmethod
    def normalize_transponder(cls, v: str | None) -> str | None:
        if v is None:
            return None
        return v.strip() or None


class TollEntry(BaseModel):
    timestamp: datetime
    plaza: str
    amount: float
    transponder_id: str | None = None
    license_plate: str | None = None


class TripTollResult(BaseModel):
    trip_id: str
    start: datetime
    end: datetime
    license_plate: str
    tolls: list[TollEntry]
    total_toll_amount: float


class MatchResponse(BaseModel):
    trips: list[TripTollResult]
    unmatched_tolls: list[EZPassToll]
