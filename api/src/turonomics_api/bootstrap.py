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

from turonomics_api.bouncie.client import BouncieClient, BouncieError
from turonomics_api.bouncie.sync import sync_vehicles
from turonomics_api.db.base import session_scope
from turonomics_api.db.models import Vehicle
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
    run_bootstrap()
    return 0  # never fail the boot


if __name__ == "__main__":
    raise SystemExit(main())
