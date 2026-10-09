# An Overpass API over a GeoDesk GOL (www/overpass): the query service only.
# The GOL it reads is built by datasvc after its sync (otmlib.gol).
# Build context is the repository root, like the other services (see
# www/docker-compose.yml).
#

FROM --platform=linux/amd64 python:3.12-slim-trixie

# curl for the compose healthcheck.
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY www/overpass/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY www/overpass /app/overpass

ENV PYTHONUNBUFFERED=1 \
    OTM_DATA_DIR=/app/data \
    OTM_OVERPASS_GOL=/app/data/gol/regions.gol

EXPOSE 8080

# Sync workers, one thread each: a query holds its worker until it is done,
# and every worker opens the GOL itself (memory-mapped, so the page cache is
# shared between them). --timeout is the hard stop above a script's own
# [timeout:], which the service caps at OTM_OVERPASS_MAX_TIMEOUT (600 s).
CMD ["sh", "-c", "exec gunicorn --chdir /app --bind 0.0.0.0:8080 --workers ${OTM_OVERPASS_WORKERS:-2} --threads 1 --timeout 660 --access-logfile - --error-logfile - 'overpass.server:create_app()'"]
