#!/bin/sh
set -eu

mkdir -p /app/data/sea /app/data/bounds /app/data/jobs \
  /app/data/geofabrik-cache /app/data/dem-cache

# One sync worker: Huey consumer lives in-process. Threads handle concurrent HTTP.
# First start downloads sea/bounds into /app/data (see logs for progress).
exec /opt/otm-venv/bin/gunicorn \
  --chdir /app \
  --bind "${OTM_HOST:-0.0.0.0}:${OTM_PORT:-8080}" \
  --workers 1 \
  --threads "${OTM_THREADS:-8}" \
  --timeout "${OTM_TIMEOUT:-600}" \
  --access-logfile - \
  --error-logfile - \
  --worker-tmp-dir /dev/shm \
  "server:create_app()"
