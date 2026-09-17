from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from turonomics_api.routers import fleet, match

app = FastAPI(
    title="Turonomics API",
    description="Match NY EZPass toll charges to Turo rental trips.",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Restrict to your frontend origin in production
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

app.include_router(match.router)
app.include_router(fleet.router)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


# The UI ships with the API rather than as a separate deploy: one service, one
# origin, no CORS to configure, and the page can never be newer than the
# endpoints it calls.
#
# Served by explicit routes rather than a catch-all mount at "/": a mount there
# matches every path, so it shadows the API's own 405s and turns "wrong method"
# into "not found".
_WEB_DIR = Path(__file__).parent / "web"

if (_WEB_DIR / "vendor").is_dir():
    app.mount("/vendor", StaticFiles(directory=_WEB_DIR / "vendor"), name="vendor")


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(_WEB_DIR / "index.html")
