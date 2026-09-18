#!/usr/bin/env sh
set -e

echo "==> applying migrations"
alembic upgrade head

# Optional, idempotent, and deliberately not covered by `set -e`: a Bouncie
# outage at deploy time should cost a stale registry, not a service that will
# not start. Migrations above are the opposite — those must stop the boot.
if [ "${BOOTSTRAP_FLEET:-}" != "" ] || [ "${BOOTSTRAP_SIGNS:-}" != "" ]; then
  echo "==> bootstrapping fleet"
  python -m turonomics_api.bootstrap || echo "==> bootstrap failed, continuing"
fi

echo "==> starting api on port ${PORT:-8000}"
exec uvicorn turonomics_api.main:app --host 0.0.0.0 --port "${PORT:-8000}"
