# Turonomics

Tooling for Turo hosts to reconcile NY EZPass toll charges against individual rental trips.

## Components

### 1. `extension/` — Chrome Browser Extension
Scrapes the Turo host dashboard and exports a CSV of all trips from the past 90 days,
including trip start time, end time, and vehicle license plate.

→ See [`extension/README.md`](extension/README.md)

### 2. `api/` — FastAPI Web Service
Accepts the Turo trips CSV (from the extension) and a NY EZPass toll transactions CSV.
Matches each toll charge to the corresponding trip and returns a per-trip breakdown.
Supports grouping multiple transponder IDs and license plates under a single owner identity.

→ See [`api/README.md`](api/README.md)

## Monorepo Layout

```
turonomics/
├── extension/          Chrome Extension (Manifest V3, TypeScript)
└── api/                FastAPI web service (Python 3.12)
```

## Quick Start

### Extension
```bash
cd extension
npm install
npm run build
# Load extension/dist/ as an unpacked extension in Chrome
```

### API
```bash
cd api
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
uvicorn turonomics_api.main:app --reload
```
