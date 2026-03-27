from datetime import datetime
from pydantic import BaseModel, field_validator


class TuroTrip(BaseModel):
    trip_id: str
    start: datetime
    end: datetime
    license_plate: str

    @field_validator("license_plate")
    @classmethod
    def normalize_plate(cls, v: str) -> str:
        return v.upper().replace(" ", "").replace("-", "")


class EZPassToll(BaseModel):
    timestamp: datetime
    plaza: str
    amount: float
    transponder_id: str
    license_plate: str | None = None

    @field_validator("license_plate")
    @classmethod
    def normalize_plate(cls, v: str | None) -> str | None:
        if v is None:
            return None
        normalized = v.upper().replace(" ", "").replace("-", "")
        return normalized or None

    @field_validator("transponder_id")
    @classmethod
    def normalize_transponder(cls, v: str) -> str:
        return v.strip()


class OwnerAliases(BaseModel):
    """Transponder IDs and license plates that belong to one owner identity."""

    transponder_ids: list[str] = []
    license_plates: list[str] = []

    @field_validator("license_plates")
    @classmethod
    def normalize_plates(cls, v: list[str]) -> list[str]:
        return [p.upper().replace(" ", "").replace("-", "") for p in v]

    @field_validator("transponder_ids")
    @classmethod
    def normalize_transponders(cls, v: list[str]) -> list[str]:
        return [t.strip() for t in v]


class TollEntry(BaseModel):
    timestamp: datetime
    plaza: str
    amount: float
    transponder_id: str


class TripTollResult(BaseModel):
    trip_id: str
    start: datetime
    end: datetime
    license_plate: str
    owner: str | None
    tolls: list[TollEntry]
    total_toll_amount: float


class MatchResponse(BaseModel):
    trips: list[TripTollResult]
    unmatched_tolls: list[EZPassToll]
