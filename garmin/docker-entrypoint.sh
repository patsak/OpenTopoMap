#!/bin/sh
set -eu

seed=/opt/otm/seed
mkdir -p /app/data/sea /app/data/bounds /app/data/jobs \
  /app/data/geofabrik-cache /app/data/dem-cache

if [ -d "$seed/sea" ] && [ -z "$(ls -A /app/data/sea 2>/dev/null || true)" ]; then
  cp -a "$seed/sea/." /app/data/sea/
fi
if [ -d "$seed/bounds" ] && [ -z "$(ls -A /app/data/bounds 2>/dev/null || true)" ]; then
  cp -a "$seed/bounds/." /app/data/bounds/
fi

/opt/otm-venv/bin/python /app/download_deps.py

# One sync worker: Huey consumer lives in-process. Threads handle concurrent HTTP.
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
