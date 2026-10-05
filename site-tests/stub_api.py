"""The API with nothing behind it, for rendering the site in CI.

Both pages are static files against a separate service, so the only way to
exercise them without deploying is to serve a believable payload. The shapes
here follow the real ``/api/fleet`` and ``/api/tolls`` responses; the values
are invented.

Deliberately includes the states that are easy to get wrong and impossible to
notice in a screenshot of the happy path: a car on a trip, a car whose parking
side is unconfirmed, tasks with and without deadlines, a multi-paragraph guest
note, and a vehicle with no tracker at all.
"""

from __future__ import annotations

import datetime as dt
import json
from http.server import BaseHTTPRequestHandler, HTTPServer

NOW = dt.datetime.now(dt.UTC)


def _iso(**delta: float) -> str:
    return (NOW + dt.timedelta(**delta)).isoformat()


VEHICLE_ID = "74cc645f-de1a-4c12-bd9e-33a396708f82"

FLEET = {
    "as_of": NOW.isoformat(),
    "untracked_count": 1,
    "fleet_timezone": "America/New_York",
    "map_tiles": {
        "url": "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
        "attribution": "&copy; OpenStreetMap contributors",
        "invert": False,
        "max_zoom": 19,
    },
    "vehicles": [
        {
            "id": VEHICLE_ID,
            "nickname": "Jolene",
            "make": "Toyota",
            "model": "Corolla",
            "year": 2025,
            "plate": "LWH4685",
            "has_tracker": True,
            "position": {"lat": 40.6779, "lon": -73.9700, "heading": 287.0,
                         "reported_at": _iso(minutes=-12)},
            "fuel_percent": 38.0,
            "odometer_miles": 24252.1,
            "battery_status": "normal",
            "on_trip_with": None,
            "trip_ends_at": None,
            "parking": {
                "session_id": "498b56d0-635c-4e18-9dc1-aa8a397d8da7",
                "since": _iso(hours=-3), "confirmed": True, "confirmed_side": "north",
                "street_name": "PROSPECT PLACE", "must_move_by": _iso(hours=2),
                "guess_from_memory": False, "times_confirmed": 1, "options": [],
            },
            "open_task_count": 2,
            "tasks": [
                {"id": "11111111-1111-1111-1111-111111111111", "kind": "asp_move",
                 "title": "Move Jolene", "detail": "street cleaning 8:30am-10am",
                 "due_by": _iso(hours=2), "location_label": "Prospect Place — north side"},
                {"id": "22222222-2222-2222-2222-222222222222", "kind": "fuel",
                 "title": "Top up the tank", "detail": "38% left",
                 "due_by": None, "location_label": None},
            ],
            "guest_notes": [
                {"received_at": _iso(minutes=-18), "guest_name": "Cambria",
                 "body": "195 prospect place\n\nNote: keys in the lockbox."},
            ],
        },
        {
            "id": "0652acd1-25f8-454f-8a94-b7cbffa27179",
            "nickname": "Jimmy", "make": "Toyota", "model": "4-Runner", "year": 2023,
            "plate": "LEH9892", "has_tracker": True,
            "position": {"lat": 42.0511, "lon": -74.0331, "heading": None,
                         "reported_at": _iso(hours=-5)},
            "fuel_percent": 65.5, "odometer_miles": 46978.3, "battery_status": "normal",
            "on_trip_with": "Michael", "trip_ends_at": _iso(hours=6),
            "parking": None, "open_task_count": 0, "tasks": [], "guest_notes": [],
        },
        {
            "id": "10a6ccef-5972-4bc0-8b73-b91a109ebd33",
            "nickname": "Jerry", "make": "Toyota", "model": "Corolla", "year": 2025,
            "plate": None, "has_tracker": False,
            "position": None, "fuel_percent": None, "odometer_miles": None,
            "battery_status": None, "on_trip_with": None, "trip_ends_at": None,
            "parking": {
                "session_id": "aaaa0000-0000-0000-0000-00000000aaaa",
                "since": _iso(hours=-1), "confirmed": False, "confirmed_side": None,
                "street_name": None, "must_move_by": None,
                "guess_from_memory": False, "times_confirmed": 0,
                "options": [
                    {"id": "bbbb0000-0000-0000-0000-00000000bbbb",
                     "street_name": "BERGEN STREET", "side": "north",
                     "distance_m": 4.2, "is_guess": True, "from_memory": False},
                    {"id": "cccc0000-0000-0000-0000-00000000cccc",
                     "street_name": "BERGEN STREET", "side": "south",
                     "distance_m": 6.8, "is_guess": False, "from_memory": False},
                ],
            },
            "open_task_count": 0, "tasks": [], "guest_notes": [],
        },
    ],
}

SPOTS = {
    "vehicle_id": VEHICLE_ID,
    "searched_from": {"lat": 40.6779, "lon": -73.9700, "heading": None,
                      "reported_at": NOW.isoformat()},
    "radius_m": 400.0,
    "spots": [
        {"segment_side_id": "aaaaaaaa-0000-0000-0000-000000000001",
         "street_name": "ST MARKS AVENUE", "side": "south",
         "distance_m": 212.4, "next_cleaning": _iso(days=3), "fits_van": True,
         "between": "6 AVENUE to 7 AVENUE"},
        {"segment_side_id": "aaaaaaaa-0000-0000-0000-000000000002",
         "street_name": "LINCOLN PLACE", "side": "north",
         "between": "8 AVENUE to 7 AVENUE",
         "distance_m": 391.0, "next_cleaning": _iso(days=3), "fits_van": None},
        # Same street, same side, one metre apart — a separate block, not a
        # duplicate. NYC splits a street into a segment per block and the only
        # thing telling these apart is the cross streets.
        {"segment_side_id": "aaaaaaaa-0000-0000-0000-000000000003",
         "street_name": "LINCOLN PLACE", "side": "north",
         "between": "PLAZA STREET to 8 AVENUE",
         "distance_m": 392.0, "next_cleaning": _iso(days=3), "fits_van": None},
    ],
}


# A transponder number that belongs to nothing. Written to look nothing like a
# real one on purpose: an earlier fixture in this repo was committed as a "real
# world example", and a later session read its tag number back out as fleet
# data. A fixture should be impossible to mistake for the thing it stands in
# for.
STUB_TAG_A = "99900000001"
STUB_TAG_B = "99900000002"
STUB_TAG_C = "99900000003"


def _toll(**over: object) -> dict:
    row = {
        "id": "dddd0000-0000-0000-0000-000000000000",
        "occurred_at": _iso(days=-3),
        "plaza": "RKB",
        "amount_cents": 911,
        "transponder_id": None,
        "license_plate": None,
        "vehicle_nickname": None,
        "guest_name": None,
        "trip_id": None,
        "recovered_at": None,
        "outside_label": None,
        "near_guest": None,
        "near_gap_seconds": None,
        "near_relation": None,
        "near_trip_id": None,
        "overrun_seconds": None,
    }
    row.update(over)
    return row


# Every case the page renders differently, because the happy path is the one
# case that cannot go unnoticed:
#   - billed to a guest
#   - charged to one of ours with nobody in it
#   - a tag nobody has bound (two, so the plural wording and the env-var line
#     both get exercised)
#   - a plate from outside the fleet
#   - one already billed back, so the tick's undo path is reachable
# The amounts are chosen to break float money: 201 cents is the $2.01 that
# int(2.01 * 100) turns into 200, and 123456 needs a thousands separator.
TOLLS = [
    _toll(id="dddd0000-0000-0000-0000-00000000001a", plaza="CRZ", amount_cents=900,
          license_plate="LEH9892", vehicle_nickname="Jimmy", guest_name="Michael",
          trip_id="eeee0000-0000-0000-0000-00000000000a"),
    _toll(id="dddd0000-0000-0000-0000-00000000002a", plaza="GWB", amount_cents=123456,
          license_plate="LWH4685", vehicle_nickname="Jolene"),
    _toll(id="dddd0000-0000-0000-0000-00000000003a", plaza="NYSTA 15 to 19",
          amount_cents=201, transponder_id=STUB_TAG_A),
    _toll(id="dddd0000-0000-0000-0000-0000000000aa", plaza="VNB", amount_cents=1263,
          license_plate="LEH9892", vehicle_nickname="Jimmy", guest_name="Priya",
          trip_id="eeee0000-0000-0000-0000-00000000000d", overrun_seconds=34 * 60),
    _toll(id="dddd0000-0000-0000-0000-00000000009a", plaza="TNB", amount_cents=688,
          license_plate="LWH4685", vehicle_nickname="Jolene",
          near_guest="Dylan", near_gap_seconds=20 * 60, near_relation="after",
          near_trip_id="eeee0000-0000-0000-0000-00000000000c"),
    _toll(id="dddd0000-0000-0000-0000-00000000004a", plaza="LNT", amount_cents=1700,
          transponder_id=STUB_TAG_A),
    _toll(id="dddd0000-0000-0000-0000-00000000005a", plaza="BER", amount_cents=217,
          transponder_id=STUB_TAG_B),
    _toll(id="dddd0000-0000-0000-0000-00000000006a", plaza="HBT", amount_cents=1700,
          license_plate="ABC1234"),
    # A crossing on the account that is not the fleet's — a family car. Real
    # money out, nobody's to repay, and no binding will fix it.
    _toll(id="dddd0000-0000-0000-0000-00000000008a", plaza="GSP", amount_cents=925,
          transponder_id=STUB_TAG_C, outside_label="Mum's car"),
    _toll(id="dddd0000-0000-0000-0000-00000000007a", plaza="WDG", amount_cents=150,
          license_plate="LZA7293", vehicle_nickname="Jerry", guest_name="Dana",
          trip_id="eeee0000-0000-0000-0000-00000000000b",
          recovered_at=_iso(days=-1)),
]


# The stub demands a token, because the page's behaviour when one is required
# is the part worth checking: it has to ask for it, keep it, and send it. Set to
# a list so the handler can record what actually arrived on the wire — a page
# that prompts and then forgets to send the header would otherwise look fine.
TOLLS_TOKEN = "letmein"
SEEN_AUTH: list[str] = []


TRIPS = [
    {"id": "eeee0000-0000-0000-0000-00000000000e", "vehicle_nickname": "Jolene",
     "guest_name": "Priya", "starts_at": _iso(days=-2), "ends_at": _iso(days=-1),
     "source": "manual", "earnings_cents": 18000, "toll_count": 3},
    # A rental that caught nothing: usually a window typed slightly wrong, and
    # the page calls it out rather than leaving it to be noticed.
    {"id": "eeee0000-0000-0000-0000-00000000000f", "vehicle_nickname": "Bubba",
     "guest_name": None, "starts_at": _iso(days=-9), "ends_at": _iso(days=-8),
     "source": "manual", "earnings_cents": None, "toll_count": 0},
]


def _tolls_payload() -> dict:
    """Recomputed per request, so a tick changes what the next load reports.

    A stub that serves a constant cannot tell a page that re-renders from the
    response apart from one that re-renders from the tap, which is the bug the
    smoke test is there to catch.
    """
    # The real API leaves a labelled tag out of this list: it is not waiting
    # for a car, so asking for a binding every month would be noise.
    unknown = sorted(
        {t["transponder_id"] for t in TOLLS
         if t["vehicle_nickname"] is None and t["transponder_id"]
         and not t["outside_label"]}
    )
    return {
        "tolls": TOLLS,
        "total_cents": sum(t["amount_cents"] for t in TOLLS),
        "unrecovered_cents": sum(t["amount_cents"] for t in TOLLS if not t["recovered_at"]),
        "unattributed_cents": sum(
            t["amount_cents"] for t in TOLLS
            if not t["trip_id"] and not t["outside_label"]
        ),
        "outside_cents": sum(
            t["amount_cents"] for t in TOLLS
            if not t["trip_id"] and t["outside_label"]
        ),
        "unknown_tags": unknown,
        "token_required": True,
    }


# Invoices: one of each case the page groups differently, because the grouping
# is the whole point of the view — what to file this week, what can wait, what
# has to be invoiced directly, and what is already lost.
def _invoice(**over: object) -> dict:
    row = {
        "trip_id": "ffff0000-0000-0000-0000-000000000001",
        "guest_name": "Dylan",
        "vehicle_nickname": "Jolene",
        "starts_at": _iso(days=-40),
        "ends_at": _iso(days=-39),
        "off_platform": False,
        "turo_trip_id": "54958910",
        "total_cents": 1555,
        "lines": [
            {"toll_id": "dddd0000-0000-0000-0000-00000000001a", "occurred_at": _iso(days=-39),
             "plaza": "RKB", "amount_cents": 911, "overrun_seconds": None},
            {"toll_id": "dddd0000-0000-0000-0000-00000000002a", "occurred_at": _iso(days=-39),
             "plaza": "GWB", "amount_cents": 644, "overrun_seconds": 34 * 60},
        ],
        "file_by": _iso(days=51),
        "days_left": 51,
        "expired": False,
        "charged_cents": 0,
        "pending_cents": 0,
        "charged_but_different": False,
        "charged_tolls_cents": None,
        "charged_lines": [],
    }
    row.update(over)
    return row


INVOICES = [
    _invoice(trip_id="ffff0000-0000-0000-0000-000000000002", guest_name="Samuel",
             days_left=6, file_by=_iso(days=6), total_cents=1666),
    _invoice(),
    _invoice(trip_id="ffff0000-0000-0000-0000-000000000003", guest_name="Priya",
             off_platform=True, turo_trip_id=None, days_left=None, file_by=None,
             total_cents=2200),
    # Turo charged a different amount on this rental: cleaning or fuel rode on
    # the same invoice, so it cannot be ticked off automatically.
    _invoice(trip_id="ffff0000-0000-0000-0000-000000000005", guest_name="Katherine",
             days_left=30, file_by=_iso(days=30), total_cents=911,
             charged_cents=5555, charged_but_different=True,
             charged_tolls_cents=1555,
             charged_lines=["Tolls $15.55", "Cleaning $40.00"]),
    _invoice(trip_id="ffff0000-0000-0000-0000-000000000006", guest_name="Austin",
             days_left=33, file_by=_iso(days=33), total_cents=4071, pending_cents=4071),
    _invoice(trip_id="ffff0000-0000-0000-0000-000000000004", guest_name="Brandon",
             days_left=-5, file_by=_iso(days=-5), expired=True, total_cents=1679),
]


def _invoices_payload() -> dict:
    return {
        "invoices": INVOICES,
        "window_days": 90,
        "billable_cents": sum(i["total_cents"] for i in INVOICES),
        "urgent_cents": sum(i["total_cents"] for i in INVOICES
                            if i["days_left"] is not None and 0 <= i["days_left"] <= 21),
        "expired_cents": sum(i["total_cents"] for i in INVOICES if i["expired"]),
        "off_platform_cents": sum(i["total_cents"] for i in INVOICES if i["off_platform"]),
        "needs_a_look_cents": sum(i["total_cents"] for i in INVOICES
                                  if i["charged_but_different"]),
        "token_required": True,
    }


class Handler(BaseHTTPRequestHandler):
    def _send(self, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:
        # The page is on another origin, so a DELETE is preflighted. Naming the
        # methods matters: the real API allows GET, POST and DELETE, and a stub
        # that waved everything through would hide a missing one.
        body = b"{}"
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path.startswith("/api/invoices"):
            self._send(_invoices_payload())
        elif self.path.startswith("/api/trips"):
            self._send({"trips": TRIPS})
        elif self.path.startswith("/seen-auth"):
            self._send({"seen": SEEN_AUTH})
        elif self.path.startswith("/api/tolls"):
            self._send(_tolls_payload())
        elif "/spots" in self.path:
            self._send(SPOTS)
        else:
            self._send(FLEET)

    def _authorized(self) -> bool:
        supplied = (self.headers.get("Authorization") or "").removeprefix("Bearer ").strip()
        SEEN_AUTH.append(supplied)
        if supplied == TOLLS_TOKEN:
            return True
        body = b'{"detail":"bad or missing token"}'
        self.send_response(401)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        return False

    def do_POST(self) -> None:
        path, _, query = self.path.partition("?")
        if (path.startswith("/api/tolls") or path.startswith("/api/trips")
                or path.startswith("/api/invoices")) and not self._authorized():
            return
        if path.startswith("/api/invoices/") and path.endswith("/recovered"):
            trip_id = path.split("/")[-2]
            for index, invoice in enumerate(INVOICES):
                if invoice["trip_id"] == trip_id:
                    self._send(INVOICES.pop(index))
                    return
            self._send({})
            return
        if path.endswith("/recovered"):
            toll_id = path.split("/")[-2]
            undo = "undo=true" in query
            for toll in TOLLS:
                if toll["id"] == toll_id:
                    toll["recovered_at"] = None if undo else NOW.isoformat()
                    self._send(toll)
                    return
            self._send({})
        elif path == "/api/tolls/import":
            # Nothing is parsed: the upload path under test is the page's, and
            # the parser has its own tests against real statement rows.
            self._read_body()
            self._send({"rows": 9, "imported": 7, "already_known": 2,
                        "matched": 4, "unmatched": 3, "unknown_tags": [STUB_TAG_A, STUB_TAG_B]})
        elif path == "/api/trips":
            self._read_body()
            self._send({"trip": TRIPS[0], "tolls_matched": 2})
        elif path == "/api/tolls/rematch":
            self._send({"rows": 3, "imported": 0, "already_known": 0,
                        "matched": 0, "unmatched": 3, "unknown_tags": []})
        else:
            self._send(FLEET["vehicles"][0])

    def do_DELETE(self) -> None:
        path, _, _query = self.path.partition("?")
        if (path.startswith("/api/tolls") or path.startswith("/api/trips")) \
                and not self._authorized():
            return
        if path.startswith("/api/trips/"):
            trip_id = path.rsplit("/", 1)[-1]
            for index, trip in enumerate(TRIPS):
                if trip["id"] == trip_id:
                    TRIPS.pop(index)
                    self._send({"deleted": 1, "tolls_released": trip["toll_count"]})
                    return
            self._send({"deleted": 0, "tolls_released": 0})
            return
        toll_id = path.rsplit("/", 1)[-1]
        for index, toll in enumerate(TOLLS):
            if toll["id"] == toll_id:
                TOLLS.pop(index)
                self._send({"deleted": 1})
                return
        self._send({"deleted": 0})

    def _read_body(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)

    def log_message(self, *args: object) -> None:
        pass


if __name__ == "__main__":
    HTTPServer(("127.0.0.1", 8910), Handler).serve_forever()
