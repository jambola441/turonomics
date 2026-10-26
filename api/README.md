# Turonomics — API

FastAPI service that matches NY EZPass toll charges to individual Turo trips.

## Endpoints

### `POST /match`

Accepts two CSV files and an alias map. Returns a per-trip toll breakdown.

**Form fields:**

| Field | Type | Description |
|-------|------|-------------|
| `turo_file` | file (CSV) | Turo trips export from the browser extension |
| `ezpass_file` | file (CSV) | NY EZPass toll transaction export |
| `aliases` | string (JSON) | Identity alias map (see below) |

**Alias map format:**

```json
{
  "John Smith": {
    "transponder_ids": ["E-ZPass-123456"],
    "license_plates": ["ABC1234", "XYZ5678"]
  },
  "Jane Doe": {
    "transponder_ids": ["E-ZPass-789012"],
    "license_plates": ["LMN9999"]
  }
}
```

An alias map is optional. If omitted, tolls are matched to trips by license
plate alone (the plate on the toll must exactly match the plate on the trip).

**Response:**

```json
{
  "trips": [
    {
      "trip_id": "12345",
      "start": "2024-01-15T10:00:00",
      "end": "2024-01-18T14:00:00",
      "license_plate": "ABC1234",
      "owner": "John Smith",
      "tolls": [
        {
          "timestamp": "2024-01-16T08:23:00",
          "plaza": "Verrazano Bridge",
          "amount": 19.0,
          "transponder_id": "E-ZPass-123456"
        }
      ],
      "total_toll_amount": 19.0
    }
  ],
  "unmatched_tolls": []
}
```

### `POST /mcp` — the books, for Claude

The API is also an MCP server: one endpoint speaking MCP's streamable HTTP
transport (a JSON-RPC request per POST, a JSON answer, no event stream), so a
Claude session can ask about the fleet's own data instead of reading code or
the site. It reads, and writes one thing: a draft message to a guest, which
nothing sends until the operator presses Send on the trips page. Eight tools:

| tool | answers |
|---|---|
| `next_to_file` | what Turo will still take a filing for, soonest deadline first |
| `ledger` | every rental and what became of its crossings, with totals; by state or guest |
| `find_trips` | rentals by guest, car, reservation number or dates; gives the `trip_id` |
| `trip` | everything about one rental (the guest's messages only when asked) |
| `tolls` | crossings: open, with no rental, or all; optionally one rental's |
| `extension_status` | whether the browser extension is listening, and its last jobs |
| `draft_message` | writes a message to a rental's guest **as a draft**; it is not sent |
| `message_drafts` | the drafts and what became of each (draft, approved, sent, failed, discarded) |

### Messages to guests (`/api/messages`)

Every message is approved by a person. A draft, from the site or from Claude,
is sent only when Send is pressed on it on the trips page, which queues a
`message` command; the extension (1.20.0 or later) posts it from a Turo tab
with Turo's own `POST /api/v2/message/send` and reports whether Turo stored it.
No MCP tool approves or sends, a `message` command cannot be queued except
from an approved draft, an older extension is never handed one, and one that
was claimed and never reported is marked failed rather than sent again.

It needs `TOLLS_TOKEN` as a bearer token, and unlike the pages it refuses to
answer at all while `TOLLS_TOKEN` is unset. In Claude Code:

```sh
claude mcp add --transport http turonomics https://turonomics.onrender.com/mcp \
  --header "Authorization: Bearer $TOLLS_TOKEN"
```

The claude.ai and mobile apps' custom connectors may need OAuth rather than a
bearer header; this endpoint does not offer OAuth yet.

## CSV Schemas

### Turo CSV (from extension)

```
trip_id,start_time,end_time,license_plate
12345,2024-01-15T10:00:00,2024-01-18T14:00:00,ABC1234
```

### NY EZPass CSV

The EZPass CSV export from `myezpass.com` (Account Activity → Download).
Expected columns (case-insensitive, order flexible):

| Column | Description |
|--------|-------------|
| `date` or `transaction date` | Date of the toll |
| `time` or `transaction time` | Time of the toll (or combined datetime) |
| `location` or `plaza` | Toll plaza name |
| `debit` or `amount` | Toll amount (numeric, may include `$`) |
| `tag` or `transponder` or `transponder id` | EZPass tag ID |
| `license plate` or `plate` | License plate recorded at plaza (optional) |

## Development

```bash
cd api
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# Run server
uvicorn turonomics_api.main:app --reload

# Tests
pytest tests/ -v

# Lint + format
ruff check src/ tests/
ruff format src/ tests/

# Type check
mypy src/
```

## Docker

```bash
docker build -t turonomics-api .
docker run -p 8000:8000 turonomics-api
```

Interactive docs: http://localhost:8000/docs
