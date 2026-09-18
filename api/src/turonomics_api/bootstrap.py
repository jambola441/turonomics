"""One-command fleet setup at container boot.

Render's database only accepts connections from inside Render, so the
alternative to this is shelling into the service to run two commands by hand.
This does the same thing from an environment variable.

Two properties make it safe to leave switched on permanently:

* **Idempotent.** Vehicles are only created for devices not already
  registered, and a plate is only written when the vehicle has none — so a
  correction made later in the app is not stomped on the next deploy.
* **Non-fatal.** Unlike migrations, a failure here does not stop the service.
  A Bouncie outage should cost you a stale registry, not an API that will not
  boot. Migrations are different: serving against a schema the code does not
  understand is worse than not serving.
"""

from __future__ import annotations

import logging
import os

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from turonomics_api.asp.ingest import ZIP_11238_BBOX, fetch_signs, load_signs
from turonomics_api.bouncie.client import BouncieClient, BouncieError
from turonomics_api.bouncie.sync import sync_vehicles
from turonomics_api.db.base import session_scope
from turonomics_api.db.models import StreetSegmentSide, Vehicle
from turonomics_api.plates import normalize_plate

log = logging.getLogger("turonomics.bootstrap")


def parse_plate_map(raw: str) -> dict[str, str]:
    """``"Jimmy=LEH9892,Jolene=LWH4685"`` -> ``{"jimmy": "LEH9892", ...}``.

    Keys are lower-cased for matching; values go through the same normalisation
    the registry applies, so a plate written here with a dash still matches a
    toll record without one.
    """
    out: dict[str, str] = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair or "=" not in pair:
            continue
        name, _, plate = pair.partition("=")
        name, plate = name.strip(), plate.strip()
        if name and plate:
            out[name.lower()] = normalize_plate(plate)
    return out


def apply_plates(session: Session, plate_map: dict[str, str]) -> list[str]:
    """Attach plates to vehicles by nickname. Returns what changed."""
    changed: list[str] = []
    if not plate_map:
        return changed

    for vehicle in session.scalars(select(Vehicle)):
        for name in filter(None, (vehicle.nickname, vehicle.bouncie_nickname)):
            plate = plate_map.get(name.lower())
            if plate is None:
                continue
            if vehicle.plate == plate:
                break
            if vehicle.plate is not None:
                # Already has a different plate: someone set it deliberately,
                # and a deploy is not the place to overrule them.
                log.info("%s already has plate %s — leaving it", vehicle.nickname, vehicle.plate)
                break
            vehicle.plate = plate
            changed.append(f"{vehicle.nickname}={plate}")
            break
    return changed


def _flag(name: str) -> str:
    return os.environ.get(name, "").strip().lower()


def _parse_bbox(raw: str) -> tuple[int, int, int, int]:
    parts = [int(v) for v in raw.split(",")]
    if len(parts) != 4:
        raise ValueError("BOOTSTRAP_SIGNS_BBOX needs four numbers: x0,y0,x1,y1")
    return parts[0], parts[1], parts[2], parts[3]


def run_sign_bootstrap() -> int:
    """Load street-cleaning rules if there are none. Returns rules created.

    Skipped when segments already exist, because the fetch pulls several
    thousand rows and re-doing it on every restart would add half a minute to
    each deploy for no change. ``BOOTSTRAP_SIGNS=force`` reloads anyway, which
    is how to pick up the monthly dataset refresh.

    Runs before the vehicle sync: a parking session resolves its candidate
    sides at the moment it opens, so the rules have to be in place first or the
    first sync produces sessions with nothing to choose between.
    """
    mode = _flag("BOOTSTRAP_SIGNS")
    if mode not in {"1", "true", "yes", "force"}:
        log.info("BOOTSTRAP_SIGNS not set — skipping")
        return 0

    try:
        with session_scope() as session:
            existing = session.scalar(select(func.count()).select_from(StreetSegmentSide)) or 0
            if existing and mode != "force":
                log.info("%d street segments already loaded — skipping", existing)
                return 0

            raw = os.environ.get("BOOTSTRAP_SIGNS_BBOX", "").strip()
            bbox = _parse_bbox(raw) if raw else ZIP_11238_BBOX
            log.info("fetching street-cleaning signs for %s ...", bbox)
            rows = fetch_signs(bbox, app_token=os.environ.get("NYC_OPEN_DATA_APP_TOKEN") or None)
            report = load_signs(session, rows)
            log.info("%s", report.summary())
            return report.rules_created
    except Exception as exc:  # noqa: BLE001 - boot convenience must not break boot
        # Same reasoning as the fleet bootstrap: an Open Data outage should
        # cost a stale rule set, not a service that will not start.
        log.warning("sign bootstrap failed, continuing without it: %s", exc)
        return 0


def run_bootstrap() -> int:
    """Returns the number of vehicles registered. Never raises."""
    if os.environ.get("BOOTSTRAP_FLEET", "").lower() not in {"1", "true", "yes"}:
        log.info("BOOTSTRAP_FLEET not set — skipping")
        return 0

    created = 0
    try:
        with session_scope() as session:
            before = session.scalar(select(func.count()).select_from(Vehicle)) or 0
            try:
                result = sync_vehicles(session, BouncieClient(session), create_missing=True)
                created = result.created
                log.info(
                    "bouncie sync: matched=%d created=%d events=%d",
                    result.matched,
                    result.created,
                    result.events,
                )
            except BouncieError as exc:
                # A stale registry is recoverable; a dead API is not.
                log.warning("bouncie sync skipped: %s", exc)

            changed = apply_plates(session, parse_plate_map(os.environ.get("BOOTSTRAP_PLATES", "")))
            if changed:
                log.info("plates set: %s", ", ".join(changed))

            after = session.scalar(select(func.count()).select_from(Vehicle)) or 0
            log.info("fleet: %d vehicles (was %d)", after, before)
    except Exception as exc:  # noqa: BLE001 - boot convenience must not break boot
        log.warning("bootstrap failed, continuing without it: %s", exc)

    return created


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "info").upper(),
        format="%(levelname)s %(name)s: %(message)s",
    )
    # Signs first: the vehicle sync opens parking sessions, and those resolve
    # their candidate sides as they are created.
    run_sign_bootstrap()
    run_bootstrap()
    return 0  # never fail the boot


if __name__ == "__main__":
    raise SystemExit(main())
