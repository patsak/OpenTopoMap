# Hiking routes on and off trail (www/routing): the service, which maps the
# compiled graphs. The graphs are built by datasvc-job, in the datasvc image
# (GRASS, the DEM cache, the extracts) - see www/docker-compose.yml. Build
# context is the repository root, like the other services.

FROM debian:13-slim

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 \
        python3-venv \
        python3-numpy \
        python3-gdal \
        curl \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY www/routing/requirements.txt /app/requirements.txt
RUN python3 -m venv --system-site-packages /opt/otm-venv \
    && /opt/otm-venv/bin/pip install --no-cache-dir -U pip \
    && /opt/otm-venv/bin/pip install --no-cache-dir -r /app/requirements.txt

COPY www/otmlib /app/otmlib
COPY www/routing /app/routing
RUN /opt/otm-venv/bin/python -c "import routing.server"

ENV PATH="/opt/otm-venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    OTM_DATA_DIR=/app/data \
    OTM_ROUTING_CONFIG=/app/routing/config.yaml \
    DATABASE_URL=postgresql://otm:otm@postgres:5432/otm

EXPOSE 8080

# One worker: the graphs are memory-mapped, so threads share the pages; the
# disk engine's A* releases the GIL (numba nogil), so they search in parallel.
CMD ["sh", "-c", "exec gunicorn --chdir /app --bind 0.0.0.0:8080 --workers 1 --threads ${OTM_ROUTING_THREADS:-4} --timeout ${OTM_ROUTING_TIMEOUT:-300} --access-logfile - --error-logfile - 'routing.server:create_app()'"]
