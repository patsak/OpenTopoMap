# OpenTopoMap Garmin map service
# Build context is the repository root (see docker-compose.yml): styles live in garmin/.
# Build:  docker compose build
# Run:    docker compose up

FROM eclipse-temurin:21-jdk-jammy

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 \
        python3-pip \
        python3-venv \
        python3-dev \
        python3-gdal \
        gdal-bin \
        libgdal-dev \
        osmium-tool \
        curl \
        ca-certificates \
        g++ \
        cmake \
        make \
        libboost-dev \
        libexpat1-dev \
        zlib1g-dev \
        libbz2-dev \
        libgeos-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# The shared volume, and not the package directory garminsvc.constants would
# otherwise default to now that the package sits in /app/garminsvc.
ENV OTM_DATA_DIR=/app/data
# Outside /app/data: that path is the shared volume, and the jars belong to the
# image. garminsvc.constants reads this.
ENV OTM_TOOLS_DIR=/opt/garmin-tools

COPY www/garminsvc/requirements-server.txt /app/requirements-server.txt
RUN python3 -m venv --system-site-packages /opt/otm-venv \
    && /opt/otm-venv/bin/pip install --no-cache-dir -U pip \
    && /opt/otm-venv/bin/pip install --no-cache-dir -r /app/requirements-server.txt

COPY www/garminsvc/ /app/garminsvc/
# Shared with tilesvc; a plain package next to garminsvc, no install step.
COPY www/otmlib /app/otmlib
# Cartography stays under garmin/; bake the bits the service needs into the image.
COPY garmin/style /garmin/style
COPY garmin/opentopomap_hike_options /garmin/opentopomap_hike_options
COPY garmin/contours_hike_options /garmin/contours_hike_options

# The versions, their checksums and which files are worth unpacking are stated
# once, in garminsvc/artifacts.py — the same code a local checkout runs, so the
# image and a developer machine cannot end up on different revisions. Nothing
# here reads the working tree: a jar left in a checkout must not reach the image
# (see .dockerignore).
RUN /opt/otm-venv/bin/python -m garminsvc.fetchdeps \
    && test -d /garmin/style/opentopomap-hike \
    # Smoke test, not a formality: a missing lib/ only surfaces once mkgmap reaches
    # for fastutil, which happens after it has already written the .typ. Compiling
    # the real TYP here is the same code path every build starts with.
    && mkdir -p /tmp/typcheck \
    && (cd /tmp/typcheck \
        && java -jar "$OTM_TOOLS_DIR/mkgmap/mkgmap.jar" --family-id=1 /garmin/style/typ/contours-hike.txt) \
    && java -jar "$OTM_TOOLS_DIR/splitter/splitter.jar" --version >/dev/null 2>&1 \
    && rm -rf /tmp/typcheck \
    && chmod +x /app/garminsvc/docker-entrypoint.sh

ENV PATH="/opt/otm-venv/bin:${PATH}" \
    OTM_HOST=0.0.0.0 \
    OTM_PORT=8080 \
    OTM_DOCKER=1 \
    OTM_GARMIN_DIR=/garmin \
    OTM_VENV_PYTHON=/opt/otm-venv/bin/python \
    JAVA_MEM=4g \
    PYTHONUNBUFFERED=1

EXPOSE 8080

ENTRYPOINT ["/app/garminsvc/docker-entrypoint.sh"]
