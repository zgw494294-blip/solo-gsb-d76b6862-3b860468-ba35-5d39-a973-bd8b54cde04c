#!/bin/sh
# Container startup: initialize schema/default pool, then launch Uvicorn.
set -eu

echo "[entrypoint] initializing database..."
python -m app.init_db

echo "[entrypoint] starting API on 0.0.0.0:${PORT:-8000}"
exec uvicorn app.main:app \
    --host 0.0.0.0 \
    --port "${PORT:-8000}" \
    --workers "${UVICORN_WORKERS:-1}" \
    --proxy-headers
