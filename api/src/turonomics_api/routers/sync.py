"""A way to drive the poll from outside the process.

The in-process loop stops when the process does, and on Render's free tier the
service spins down after a quarter hour with no traffic — which is most of the
night, exactly when a car is sitting on a street that gets swept at 8:30am. An
external caller on a schedule both wakes the service and guarantees the poll.

Guarded by a shared token rather than left open: an unauthenticated endpoint
here would let anyone burn the Bouncie rate limit the fleet depends on. With no
token configured the endpoint reports itself unavailable, so the failure mode
of forgetting to set one is a closed door rather than an open one.
"""

from __future__ import annotations

import hmac
import os
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from turonomics_api.db.base import get_session
from turonomics_api.ingest.poller import poll_once

router = APIRouter(prefix="/api", tags=["sync"])

DbSession = Annotated[Session, Depends(get_session)]


class SyncResponse(BaseModel):
    events: int
    reconciled: int


def _check_token(authorization: str | None) -> None:
    expected = os.environ.get("SYNC_TOKEN", "").strip()
    if not expected:
        raise HTTPException(503, "sync endpoint is not configured; set SYNC_TOKEN")
    supplied = (authorization or "").removeprefix("Bearer ").strip()
    # Constant time: a token is a secret, and a timing oracle is a slow leak.
    if not supplied or not hmac.compare_digest(supplied, expected):
        raise HTTPException(401, "bad or missing token")


@router.post("/sync", response_model=SyncResponse)
def trigger_sync(
    session: DbSession,
    authorization: Annotated[str | None, Header()] = None,
) -> SyncResponse:
    _check_token(authorization)
    result = poll_once(session)
    return SyncResponse(events=result.events, reconciled=result.reconciled)
