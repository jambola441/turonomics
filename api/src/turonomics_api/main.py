from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from turonomics_api.routers import match

app = FastAPI(
    title="Turonomics API",
    description="Match NY EZPass toll charges to Turo rental trips.",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Restrict to your frontend origin in production
    allow_methods=["POST"],
    allow_headers=["*"],
)

app.include_router(match.router)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
