"""Admin commands.

Fleet data — plates, nicknames, which cars need a large parking spot — is data,
not code. It lives in the database and is entered through these commands, so
nothing identifying gets committed to the repository.

    python -m turonomics_api.cli vehicles
    python -m turonomics_api.cli sync --create-missing
    python -m turonomics_api.cli set-plate Jimmy LEH9892
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from turonomics_api.bootstrap import run_bootstrap
from turonomics_api.bouncie.client import BouncieClient
from turonomics_api.bouncie.sync import sync_vehicles
from turonomics_api.db.base import session_scope
from turonomics_api.db.models import TelemetryEvent, Vehicle


def _find(session: Session, needle: str) -> Vehicle:
    vehicle = session.scalar(
        select(Vehicle).where(
            or_(
                func.lower(Vehicle.nickname) == needle.lower(),
                func.lower(Vehicle.bouncie_nickname) == needle.lower(),
                Vehicle.plate == needle.upper().replace(" ", "").replace("-", ""),
            )
        )
    )
    if vehicle is None:
        raise SystemExit(f"no vehicle matching {needle!r}")
    assert isinstance(vehicle, Vehicle)
    return vehicle


def cmd_vehicles(_args: argparse.Namespace) -> int:
    with session_scope() as session:
        rows = session.scalars(select(Vehicle).order_by(Vehicle.nickname)).all()
        if not rows:
            print("no vehicles yet — try: sync --create-missing")
            return 0
        untracked = 0
        for v in rows:
            latest = session.scalar(
                select(TelemetryEvent)
                .where(TelemetryEvent.vehicle_id == v.id)
                .order_by(TelemetryEvent.occurred_at.desc())
                .limit(1)
            )
            tracker = v.bouncie_imei[-4:] if v.bouncie_imei else "—"
            if not v.bouncie_imei:
                untracked += 1
            plate = v.plate or "(none yet — tolls will not match)"
            print(f"  {v.nickname:10} {v.year} {v.make} {v.model:12} plate={plate}")
            print(f"  {'':10} tracker={tracker}  fuel={v.reports_fuel_level}  odo={v.reports_obd_odometer}"
                  f"  large-spot={v.needs_large_spot}")
            if latest:
                print(f"  {'':10} last seen {latest.occurred_at:%Y-%m-%d %H:%M} ({latest.event_type})")
        print(f"\n{len(rows)} vehicles · {untracked} untracked")
    return 0


def cmd_bootstrap(_args: argparse.Namespace) -> int:
    run_bootstrap()
    return cmd_vehicles(_args)


def cmd_sync(args: argparse.Namespace) -> int:
    with session_scope() as session:
        result = sync_vehicles(session, BouncieClient(session), create_missing=args.create_missing)
        print(f"matched={result.matched} created={result.created} events={result.events}")
        if result.unmatched_imeis:
            tails = ", ".join(i[-4:] for i in result.unmatched_imeis)
            print(f"devices on the account with no fleet vehicle: {tails}")
            print("re-run with --create-missing to add them")
    return 0


def cmd_set_plate(args: argparse.Namespace) -> int:
    with session_scope() as session:
        vehicle = _find(session, args.vehicle)
        before = vehicle.plate
        vehicle.plate = args.plate  # normalised by the model
        session.flush()
        print(f"{vehicle.nickname}: {before} -> {vehicle.plate}")
    return 0


def cmd_set(args: argparse.Namespace) -> int:
    with session_scope() as session:
        vehicle = _find(session, args.vehicle)
        if args.nickname:
            vehicle.nickname = args.nickname
        if args.large_spot is not None:
            vehicle.needs_large_spot = args.large_spot
        session.flush()
        print(f"{vehicle.nickname}: nickname={vehicle.nickname} large-spot={vehicle.needs_large_spot}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="turonomics_api.cli", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("vehicles", help="list the fleet").set_defaults(func=cmd_vehicles)

    sub.add_parser(
        "bootstrap",
        help="run the boot-time fleet setup by hand (honours BOOTSTRAP_FLEET)",
    ).set_defaults(func=cmd_bootstrap)

    p_sync = sub.add_parser("sync", help="pull vehicle state from Bouncie")
    p_sync.add_argument("--create-missing", action="store_true",
                        help="add a fleet vehicle for each device not already registered")
    p_sync.set_defaults(func=cmd_sync)

    p_plate = sub.add_parser("set-plate", help="set a vehicle's licence plate")
    p_plate.add_argument("vehicle", help="nickname or current plate")
    p_plate.add_argument("plate")
    p_plate.set_defaults(func=cmd_set_plate)

    p_set = sub.add_parser("set", help="edit vehicle attributes")
    p_set.add_argument("vehicle")
    p_set.add_argument("--nickname")
    p_set.add_argument("--large-spot", dest="large_spot", action=argparse.BooleanOptionalAction,
                       help="vehicle does not fit every on-street spot")
    p_set.set_defaults(func=cmd_set)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result: int = args.func(args)
    return result


if __name__ == "__main__":
    sys.exit(main())
