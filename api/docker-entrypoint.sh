#!/usr/bin/env sh
set -e

echo "==> applying migrations"
alembic upgrade head

echo "==> starting api on port ${PORT:-8000}"
exec uvicorn turonomics_api.main:app --host 0.0.0.0 --port "${PORT:-8000}"
