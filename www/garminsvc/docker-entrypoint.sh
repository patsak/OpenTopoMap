#!/bin/sh
set -eu

# Same tree tilesvc fills; OTM_DATA_DIR points both services at the shared volume.
data_dir="${OTM_DATA_DIR:-/app/data}"
mkdir -p "$data_dir/sea" "$data_dir/bounds" "$data_dir/jobs" \
  "$data_dir/geofabrik-cache" "$data_dir/dem-cache"

# Job records and the queue are both in Postgres, so there is nothing to serve
# until it answers. gunicorn would otherwise start, fail every request, and
# report itself healthy-ish while the database is still booting.
if [ -n "${DATABASE_URL:-}" ]; then
  echo "Waiting for Postgres…"
  for _ in $(seq 1 60); do
    if PYTHONPATH=/app /opt/otm-venv/bin/python -c "import otmlib.pg; otmlib.pg.connect().close()" 2>/dev/null; then
      break
    fi
    sleep 2
  done
fi

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
