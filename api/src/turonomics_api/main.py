import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from turonomics_api.routers import fleet, match

app = FastAPI(
    title="Turonomics API",
    description="Match NY EZPass toll charges to Turo rental trips.",
    version="0.1.0",
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


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}

