import json

from fastapi import APIRouter, Form, HTTPException, UploadFile
from pydantic import ValidationError

from turonomics_api.matching import match_tolls_to_trips
from turonomics_api.models import MatchResponse, OwnerAliases
from turonomics_api.parsing.ezpass import parse_ezpass_csv
from turonomics_api.parsing.turo import parse_turo_csv

router = APIRouter()


@router.post("/match", response_model=MatchResponse)
async def match(
    turo_file: UploadFile,
    ezpass_file: UploadFile,
    aliases: str = Form(default="{}"),
) -> MatchResponse:
    """Match EZPass tolls to Turo trips.

    - **turo_file**: CSV exported by the Turonomics Chrome extension.
    - **ezpass_file**: NY EZPass account activity CSV download.
    - **aliases**: JSON object mapping owner names to their transponder IDs and
      license plates. Optional — omit or pass `{}` to match by plate only.
    """
    # Parse alias map
    try:
        raw_aliases: dict[str, object] = json.loads(aliases)
        alias_map = {
            owner: OwnerAliases.model_validate(identity)
            for owner, identity in raw_aliases.items()
        }
    except (json.JSONDecodeError, ValidationError) as exc:
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
