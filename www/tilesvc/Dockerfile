# OpenTopoMap vector tile service.
# Geofabrik sync (osmium) + tilemaker build of otm.mbtiles / otm-ocean.mbtiles,
# on a supercronic schedule.

FROM debian:12-slim

ENV DEBIAN_FRONTEND=noninteractive

# The five tilemaker runtime libraries below are what its binary is dynamically
# linked against; upstream builds it on debian:bookworm-slim, the same base as
# this image, so the copied binary finds exactly these sonames. If a tilemaker
# bump ever fails to start with a missing .so, run
#   docker run --rm --entrypoint sh ghcr.io/systemed/tilemaker:master \
#     -c "ldd /usr/src/app/tilemaker"
# and add whatever it names.
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 \
        python3-pip \
        python3-venv \
        osmium-tool \
        gdal-bin \
        curl \
        ca-certificates \
        libpq5 \
        libboost-filesystem1.74.0 \
        libboost-program-options1.74.0 \
        liblua5.1-0 \
        libshp2 \
        libsqlite3-0 \
    && rm -rf /var/lib/apt/lists/*

# tilemaker has no Debian package. Upstream publishes a ready binary in its own
# image, so take that rather than compiling boost/protobuf here on every build.
COPY --from=ghcr.io/systemed/tilemaker:master /usr/src/app/tilemaker /usr/local/bin/tilemaker

# v0.2.33 crash-loops as PID 1 with "Failed to fork exec: no such file or
# directory": its reaper re-execs itself assuming cwd '/', which breaks
# when supercronic lives in a PATH dir like /usr/local/bin instead
# (aptible/supercronic#177). Fixed in v0.2.36; this pins past that.
ARG SUPERCRONIC_VERSION=v0.2.49
ARG TARGETARCH
RUN arch="${TARGETARCH:-$(dpkg --print-architecture)}" \
    && curl -fsSL -o /usr/local/bin/supercronic \
      "https://github.com/aptible/supercronic/releases/download/${SUPERCRONIC_VERSION}/supercronic-linux-${arch}" \
    && chmod +x /usr/local/bin/supercronic

WORKDIR /app

COPY www/tilesvc/requirements.txt /app/requirements.txt
RUN python3 -m venv --system-site-packages /opt/otm-venv \
    && /opt/otm-venv/bin/pip install --no-cache-dir -U pip \
    && /opt/otm-venv/bin/pip install --no-cache-dir -r /app/requirements.txt

COPY www/tilesvc /app/tilesvc
COPY www/otmlib /app/otmlib
COPY vector/tilemaker /app/vector/tilemaker
RUN cp /app/tilesvc/crontab /app/crontab \
    && tilemaker --help > /dev/null

ENV PATH="/opt/otm-venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    OTM_DATA_DIR=/app/data \
    OTM_TILESVC_CONFIG=/app/tilesvc/config.yaml \
    DATABASE_URL=postgresql://otm:otm@postgres:5432/otm

CMD ["supercronic", "/app/crontab"]
