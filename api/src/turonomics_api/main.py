import asyncio
import contextlib
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from turonomics_api.db.base import session_scope
from turonomics_api.ingest.poller import PollResult, interval_minutes, poll_once
from turonomics_api.routers import fleet, match, sync

log = logging.getLogger("turonomics.poller")


async def _poll_forever(minutes: int) -> None:
    """Pull from Bouncie on a timer for as long as the process lives.

    Sleeps before the first poll rather than after: boot already syncs, so
    polling immediately would duplicate that work on every restart.

    The poll is blocking (a database session and an HTTP call), so it runs in a
    worker thread; doing it on the event loop would stall every request for its
    duration.
    """
    while True:
        await asyncio.sleep(minutes * 60)
        try:
            result = await asyncio.to_thread(_poll_blocking)
            log.info("poll: %s", result.summary())
        except Exception as exc:  # noqa: BLE001 - a bad poll must not end the loop
            log.warning("poll failed, will retry in %dm: %s", minutes, exc)


def _poll_blocking() -> PollResult:
    with session_scope() as session:
        return poll_once(session)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    minutes = interval_minutes()
    task: asyncio.Task[None] | None = None
    if minutes:
        task = asyncio.create_task(_poll_forever(minutes))
        log.info("polling Bouncie every %d minutes", minutes)
    else:
        log.info("SYNC_INTERVAL_MINUTES=0 — not polling")
    try:
        yield
    finally:
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


app = FastAPI(
    title="Turonomics API",
    description="Match NY EZPass toll charges to Turo rental trips.",
    version="0.1.0",
    lifespan=lifespan,
)

# The UI is a separate static site, so these calls are cross-origin. Named
# origins rather than "*": the API is about to hold fleet positions and trip
# history, and a wildcard would let any page in any tab read them.
_ALLOWED_ORIGINS = [
    o.strip()
    for o in os.environ.get(
        "CORS_ALLOW_ORIGINS",
        "https://turonomics-site.onrender.com,http://localhost:8080,http://127.0.0.1:8080",
    ).split(",")
    if o.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_ALLOWED_ORIGINS,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

app.include_router(match.router)
app.include_router(fleet.router)
app.include_router(sync.router)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}

