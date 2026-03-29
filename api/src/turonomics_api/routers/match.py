import json

from fastapi import APIRouter, Form, HTTPException, UploadFile

from turonomics_api.matching import AliasMap, match_tolls_to_trips
from turonomics_api.models import MatchResponse
from turonomics_api.parsing.ezpass import parse_ezpass_csv
from turonomics_api.parsing.turo import parse_turo_csv

router = APIRouter()


def _normalize_plate(p: str) -> str:
    return p.upper().replace(" ", "").replace("-", "")


@router.post("/match", response_model=MatchResponse)
async def match(
    turo_file: UploadFile,
    ezpass_file: UploadFile,
    aliases: str = Form(default="{}"),
) -> MatchResponse:
    """Match EZPass tolls to Turo trips.

    - **turo_file**: CSV exported by the Turonomics Chrome extension.
    - **ezpass_file**: NY EZPass account activity CSV download.
    - **aliases**: JSON object mapping license plates to transponder IDs.
      Example: `{"ABC1234": "00414500433"}`. Optional — omit or pass `{}`.
    """
    # Parse alias map
    try:
        raw: dict[str, object] = json.loads(aliases)
        if not isinstance(raw, dict):
            raise ValueError("aliases must be a JSON object")
        alias_map: AliasMap = {
            _normalize_plate(str(plate)): str(tid).strip()
            for plate, tid in raw.items()
        }
    except (json.JSONDecodeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=f"Invalid aliases JSON: {exc}") from exc

    # Read uploaded files
    turo_bytes = await turo_file.read()
    ezpass_bytes = await ezpass_file.read()

    # Parse CSVs
    try:
        trips = parse_turo_csv(turo_bytes)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"Turo CSV error: {exc}") from exc

    try:
        tolls = parse_ezpass_csv(ezpass_bytes)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"EZPass CSV error: {exc}") from exc

    return match_tolls_to_trips(trips, tolls, alias_map or None)
