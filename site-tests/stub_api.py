"""A fleet API with nothing behind it, for rendering the site in CI.

The fleet view is a static page against a separate service, so the only way to
exercise it without deploying is to serve it a believable payload. The shapes
here follow the real ``/api/fleet`` response; the values are invented.

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
         "street_name": "ST MARKS AVENUE", "side": "south", "between": None,
         "distance_m": 212.4, "next_cleaning": _iso(days=3), "fits_van": True},
        {"segment_side_id": "aaaaaaaa-0000-0000-0000-000000000002",
         "street_name": "BERGEN STREET", "side": "north", "between": None,
         "distance_m": 98.1, "next_cleaning": _iso(days=1), "fits_van": None},
    ],
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
        self._send({})

    def do_GET(self) -> None:
        self._send(SPOTS if "/spots" in self.path else FLEET)

    def do_POST(self) -> None:
        self._send(FLEET["vehicles"][0])

    def log_message(self, *args: object) -> None:
        pass


if __name__ == "__main__":
    HTTPServer(("127.0.0.1", 8910), Handler).serve_forever()
