"""
Parser for NY EZPass toll transaction CSV exports.

The NY EZPass account activity CSV (downloaded from myezpass.com →
Account Activity → Download) has the following columns:

    Lane Txn ID  — transaction identifier (not used for matching)
    Tag/Plate #  — EITHER a transponder tag number OR a license plate:
                     numeric string (after stripping whitespace) → transponder
                     alphanumeric with state prefix (e.g. "NY LZA7293") → plate
    Agency       — toll authority (NYSTA, MTAB&T, GSP, PANYNJ, CBDTP, …)
    Entry Plaza  — entry point (may be empty for barrier-free plazas)
    Exit Plaza   — exit/transaction point; "PAYMENT" indicates a credit row
    Class        — vehicle class code (ignored)
    Date         — MM/DD/YYYY
    Exit Time    — HH:MM:SS AM/PM
    Amount       — dollar amount; NEGATIVE values are toll charges,
                   POSITIVE values are account payments/credits (skip these)

Example row (transponder tag):
    "33232151931"," 00414500433","NYSTA","15","19","2L","12/29/2025","05:13:32 PM","$-2.86"

Example row (license plate):
    "33237138399","NY LZA7293","MTAB&T","","RKB","31","12/31/2025","03:10:36 PM","$-9.11"

Example payment row (skip):
    ""," ","","","PAYMENT","","12/29/2025","","$25.00"
"""

import csv
import io
import re
from datetime import datetime

from turonomics_api.models import EZPassToll

# ---------------------------------------------------------------------------
# Column name → canonical key mapping (lowercase, spaces→underscores)
# ---------------------------------------------------------------------------
_COL_MAP: dict[str, list[str]] = {
    "date":        ["date"],
    "time":        ["exit_time", "time", "transaction_time"],
    "exit_plaza":  ["exit_plaza", "exit_point", "plaza"],
    "entry_plaza": ["entry_plaza", "entry_point"],
    "agency":      ["agency", "authority"],
    "tag_plate":   ["tag/plate_#", "tag/plate", "tag_plate_#", "tag_plate",
                    "tag_plate_number", "transponder/plate"],
    "amount":      ["amount", "debit", "charge", "fee"],
}

_PAYMENT_MARKERS = {"payment", "pay", "credit", "replenishment"}


def _resolve_headers(raw_headers: list[str]) -> dict[str, str]:
    normalized = {re.sub(r"\s+", "_", h.strip().lower()): h for h in raw_headers}
    resolved: dict[str, str] = {}
    for canonical, aliases in _COL_MAP.items():
        for alias in aliases:
            if alias in normalized:
                resolved[canonical] = normalized[alias]
                break
    return resolved


def _parse_amount(value: str) -> float:
    """Strip currency symbols, return absolute value. Returns 0.0 for unparseable."""
    cleaned = re.sub(r"[^\d.\-]", "", value.strip())
    if not cleaned or cleaned == "-":
        return 0.0
    return float(cleaned)  # preserves sign; caller checks sign


def _parse_datetime(date_str: str, time_str: str = "") -> datetime:
    combined = f"{date_str.strip()} {time_str.strip()}".strip()
    for fmt in (
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
    ):
        try:
            return datetime.strptime(combined, fmt)
        except ValueError:
            continue
    raise ValueError(f"Unrecognized EZPass datetime: {combined!r}")


def _classify_tag_plate(value: str) -> tuple[str | None, str | None]:
    """Return (transponder_id, license_plate) from the Tag/Plate # field.

    Detection rule:
      - Strip all whitespace from the value.
      - If the result is all digits → transponder ID.
      - Otherwise → license plate (normalize: uppercase, remove spaces/hyphens).
    """
    stripped = value.strip()
    if not stripped:
        return None, None
    digits_only = re.sub(r"\s", "", stripped)
    if digits_only.isdigit():
        return digits_only, None  # transponder
    # License plate: uppercase, drop spaces, dots, hyphens, middle-dots (·)
    plate = re.sub(r"[\s\-\.\·]", "", stripped).upper()
    return None, plate or None


def parse_ezpass_csv(content: str | bytes) -> list[EZPassToll]:
    """Parse a NY EZPass account activity CSV and return EZPassToll objects.

    Skips:
    - Payment/credit rows (Exit Plaza == "PAYMENT", or amount is positive)
    - Rows with zero or missing amounts

    Args:
        content: Raw CSV bytes or string (UTF-8, with or without BOM).

    Returns:
        List of validated EZPassToll objects (toll charges only).

    Raises:
        ValueError: If required columns are missing or a row cannot be parsed.
    """
    if isinstance(content, bytes):
        content = content.decode("utf-8-sig")

    reader = csv.DictReader(io.StringIO(content))
    if reader.fieldnames is None:
        raise ValueError("EZPass CSV appears to be empty.")

    headers = _resolve_headers(list(reader.fieldnames))

    required = {"date", "exit_plaza", "amount", "tag_plate"}
    missing = required - set(headers)
    if missing:
        raise ValueError(
            f"EZPass CSV is missing required columns: {', '.join(sorted(missing))}. "
            f"Found columns: {', '.join(str(f) for f in reader.fieldnames)}"
        )

    tolls: list[EZPassToll] = []

    for i, row in enumerate(reader, start=2):
        try:
            exit_plaza = row[headers["exit_plaza"]].strip()

            # Skip payment / credit rows
            if exit_plaza.lower() in _PAYMENT_MARKERS:
                continue

            raw_amount = _parse_amount(row[headers["amount"]])
            if raw_amount >= 0:
                # Positive = payment credit; zero = empty row — skip both
                continue
            amount = abs(raw_amount)

            date_str = row[headers["date"]].strip()
            time_str = row.get(headers.get("time", ""), "").strip() if "time" in headers else ""
            timestamp = _parse_datetime(date_str, time_str)

            # Plaza: prefer Exit Plaza; fall back to Agency when exit is empty
            agency = row.get(headers.get("agency", ""), "").strip() if "agency" in headers else ""
            entry_plaza = row.get(headers.get("entry_plaza", ""), "").strip() if "entry_plaza" in headers else ""
            plaza = exit_plaza or agency or entry_plaza
            if not plaza:
                raise ValueError("could not determine plaza name")

            tag_plate_raw = row[headers["tag_plate"]]
            transponder_id, license_plate = _classify_tag_plate(tag_plate_raw)

            tolls.append(
                EZPassToll(
                    timestamp=timestamp,
                    plaza=plaza,
                    amount=amount,
                    transponder_id=transponder_id,
                    license_plate=license_plate,
                )
            )

        except (KeyError, ValueError) as exc:
            raise ValueError(f"EZPass CSV row {i}: {exc}") from exc

    return tolls
