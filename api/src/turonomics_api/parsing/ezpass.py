"""
Parser for NY EZPass toll transaction CSV exports.

NY EZPass account activity can be downloaded from myezpass.com under
Account Activity → Download. The exported file has inconsistent column names
across different account types; this parser handles the common variants.

Known column name variants (case-insensitive):
    Date:           "Date", "Transaction Date"
    Time:           "Time", "Transaction Time"  (or combined in Date column)
    Plaza:          "Location", "Plaza", "Agency", "Description"
    Amount:         "Debit", "Amount", "Charge", "Fee"
    Transponder:    "Tag", "Transponder", "Transponder ID", "Tag ID"
    License plate:  "License Plate", "Plate", "Vehicle Plate"  (optional)
"""

import csv
import io
import re
from datetime import datetime

from turonomics_api.models import EZPassToll


_COL_MAP = {
    "date": ["date", "transaction_date", "trans_date"],
    "time": ["time", "transaction_time", "trans_time"],
    "plaza": ["location", "plaza", "agency", "description", "transaction_description"],
    "amount": ["debit", "amount", "charge", "fee", "toll_amount"],
    "transponder_id": ["tag", "transponder", "transponder_id", "tag_id", "tag_number"],
    "license_plate": ["license_plate", "plate", "vehicle_plate", "license"],
}


def _resolve_headers(raw_headers: list[str]) -> dict[str, str]:
    normalized = {
        re.sub(r"\s+", "_", h.strip().lower()): h for h in raw_headers
    }
    resolved: dict[str, str] = {}
    for canonical, aliases in _COL_MAP.items():
        for alias in aliases:
            if alias in normalized:
                resolved[canonical] = normalized[alias]
                break
    return resolved


def _parse_amount(value: str) -> float:
    """Strip currency symbols and parse as float. Negative values are credits."""
    cleaned = re.sub(r"[^\d.\-]", "", value.strip())
    if not cleaned:
        return 0.0
    return abs(float(cleaned))  # tolls are always positive charges


def _parse_datetime(date_str: str, time_str: str = "") -> datetime:
    """Parse date (and optional time) strings into a datetime."""
    combined = f"{date_str.strip()} {time_str.strip()}".strip()
    formats = [
        "%m/%d/%Y %I:%M:%S %p",
        "%m/%d/%Y %I:%M %p",
        "%m/%d/%Y %H:%M:%S",
        "%m/%d/%Y %H:%M",
        "%m/%d/%Y",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
    ]
    for fmt in formats:
        try:
            return datetime.strptime(combined, fmt)
        except ValueError:
            continue
    raise ValueError(f"Unrecognized EZPass datetime: {combined!r}")


def parse_ezpass_csv(content: str | bytes) -> list[EZPassToll]:
    """Parse a NY EZPass account activity CSV and return EZPassToll objects.

    Args:
        content: Raw CSV bytes or string.

    Returns:
        List of validated EZPassToll objects (debit transactions only).

    Raises:
        ValueError: If required columns are missing or a row cannot be parsed.
    """
    if isinstance(content, bytes):
        content = content.decode("utf-8-sig")

    reader = csv.DictReader(io.StringIO(content))
    if reader.fieldnames is None:
        raise ValueError("EZPass CSV appears to be empty.")

    headers = _resolve_headers(list(reader.fieldnames))

    required = {"date", "plaza", "amount", "transponder_id"}
    missing = required - set(headers)
    if missing:
        raise ValueError(
            f"EZPass CSV is missing required columns: {', '.join(sorted(missing))}. "
            f"Found: {', '.join(reader.fieldnames)}"
        )

    tolls: list[EZPassToll] = []
    for i, row in enumerate(reader, start=2):
        try:
            date_str = row[headers["date"]].strip()
            time_str = row.get(headers.get("time", ""), "").strip() if "time" in headers else ""
            timestamp = _parse_datetime(date_str, time_str)

            amount = _parse_amount(row[headers["amount"]])
            if amount == 0.0:
                # Skip credits and zero-amount rows
                continue

            plaza = row[headers["plaza"]].strip()
            transponder_id = row[headers["transponder_id"]].strip()

            plate_col = headers.get("license_plate")
            license_plate = row[plate_col].strip() if plate_col and plate_col in row else None

            tolls.append(
                EZPassToll(
                    timestamp=timestamp,
                    plaza=plaza,
                    amount=amount,
                    transponder_id=transponder_id,
                    license_plate=license_plate or None,
                )
            )
        except (KeyError, ValueError) as exc:
            raise ValueError(f"EZPass CSV row {i}: {exc}") from exc

    return tolls
