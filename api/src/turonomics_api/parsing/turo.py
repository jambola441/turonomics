"""
Parser for the Turo trips CSV exported by the Turonomics Chrome extension.

Expected columns (case-insensitive):
    trip_id, start_time, end_time, license_plate
"""

import csv
import io
from datetime import datetime

from turonomics_api.models import TuroTrip


# Accepted column name aliases (lowercase)
_COL_MAP = {
    "trip_id": ["trip_id", "tripid", "id", "reservation_id", "reservationid"],
    "start_time": ["start_time", "starttime", "start", "trip_start"],
    "end_time": ["end_time", "endtime", "end", "trip_end"],
    "license_plate": [
        "license_plate",
        "licenseplate",
        "plate",
        "license",
        "vehicle_plate",
    ],
}


def _resolve_headers(raw_headers: list[str]) -> dict[str, str]:
    """Map canonical field names → actual CSV column names."""
    normalized = {h.strip().lower().replace(" ", "_"): h for h in raw_headers}
    resolved: dict[str, str] = {}
    for canonical, aliases in _COL_MAP.items():
        for alias in aliases:
            if alias in normalized:
                resolved[canonical] = normalized[alias]
                break
    return resolved


def _parse_datetime(value: str) -> datetime:
    """Parse ISO 8601 or common date formats produced by the extension."""
    value = value.strip()
    # Try ISO 8601 variants
    for fmt in (
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%m/%d/%Y %H:%M",
        "%m/%d/%Y %I:%M %p",
    ):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    raise ValueError(f"Unrecognized datetime format: {value!r}")


def parse_turo_csv(content: str | bytes) -> list[TuroTrip]:
    """Parse a Turo CSV export and return a list of TuroTrip objects.

    Args:
        content: Raw CSV bytes or string.

    Returns:
        List of validated TuroTrip objects.

    Raises:
        ValueError: If required columns are missing or a row cannot be parsed.
    """
    if isinstance(content, bytes):
        content = content.decode("utf-8-sig")  # strip BOM if present

    reader = csv.DictReader(io.StringIO(content))
    if reader.fieldnames is None:
        raise ValueError("Turo CSV appears to be empty.")

    headers = _resolve_headers(list(reader.fieldnames))
    required = {"start_time", "end_time", "license_plate"}
    missing = required - set(headers)
    if missing:
        raise ValueError(
            f"Turo CSV is missing required columns: {', '.join(sorted(missing))}. "
            f"Found: {', '.join(reader.fieldnames)}"
        )

    trips: list[TuroTrip] = []
    for i, row in enumerate(reader, start=2):  # start=2 because row 1 is headers
        try:
            trip_id = row.get(headers.get("trip_id", ""), "").strip() or f"row-{i}"
            start = _parse_datetime(row[headers["start_time"]])
            end = _parse_datetime(row[headers["end_time"]])
            plate = row[headers["license_plate"]].strip()

            if not plate:
                raise ValueError("license_plate is empty")
            if end <= start:
                raise ValueError(f"end_time ({end}) is not after start_time ({start})")

            trips.append(
                TuroTrip(trip_id=trip_id, start=start, end=end, license_plate=plate)
            )
        except (KeyError, ValueError) as exc:
            raise ValueError(f"Turo CSV row {i}: {exc}") from exc

    return trips
