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
        unzip \
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

ARG MKGMAP_VERSION=mkgmap-r4924
ARG SPLITTER_VERSION=splitter-r654

WORKDIR /app

COPY www/garminsvc/requirements-server.txt /app/requirements-server.txt
RUN python3 -m venv --system-site-packages /opt/otm-venv \
    && /opt/otm-venv/bin/pip install --no-cache-dir -U pip \
    && /opt/otm-venv/bin/pip install --no-cache-dir -r /app/requirements-server.txt

COPY www/garminsvc/ /app/
# Shared with tilesvc; a plain package next to garminsvc, no install step.
COPY www/otmlib /app/otmlib
# Cartography stays under garmin/; bake the bits the service needs into the image.
COPY garmin/style /garmin/style
COPY garmin/opentopomap_hike_options /garmin/opentopomap_hike_options
COPY garmin/contours_hike_options /garmin/contours_hike_options

# Both distributions are installed whole, from the pinned versions above: the jars
# declare their dependencies through a manifest Class-Path pointing at a sibling
# lib/, so a jar on its own is not a usable tool. Nothing here reads the working
# tree — the image an ARG describes must not depend on what a checkout happens to
# contain (see .dockerignore).
RUN mkdir -p /app/tools \
    && curl --retry 5 --retry-all-errors -fsSL -o /tmp/mkgmap.zip \
         "https://www.mkgmap.org.uk/download/${MKGMAP_VERSION}.zip" \
    && unzip -q /tmp/mkgmap.zip -d /app/tools \
    && ln -sfn "/app/tools/${MKGMAP_VERSION}/mkgmap.jar" /app/tools/mkgmap.jar \
    && curl --retry 5 --retry-all-errors -fsSL -o /tmp/splitter.zip \
         "https://www.mkgmap.org.uk/download/${SPLITTER_VERSION}.zip" \
    && unzip -q /tmp/splitter.zip -d /app/tools \
    && ln -sfn "/app/tools/${SPLITTER_VERSION}/splitter.jar" /app/tools/splitter.jar \
    && rm -f /tmp/mkgmap.zip /tmp/splitter.zip \
    && test -d /garmin/style/opentopomap-hike \
    # Smoke test, not a formality: a missing lib/ only surfaces once mkgmap reaches
    # for fastutil, which happens after it has already written the .typ. Compiling
    # the real TYP here is the same code path every build starts with.
    && mkdir -p /tmp/typcheck \
    && (cd /tmp/typcheck \
        && java -jar /app/tools/mkgmap.jar --family-id=1 /garmin/style/typ/contours-hike.txt) \
    && java -jar /app/tools/splitter.jar --version >/dev/null 2>&1 \
    && rm -rf /tmp/typcheck \
    && chmod +x /app/docker-entrypoint.sh

ENV PATH="/opt/otm-venv/bin:${PATH}" \
    OTM_HOST=0.0.0.0 \
    OTM_PORT=8080 \
    OTM_DOCKER=1 \
    OTM_GARMIN_DIR=/garmin \
    OTM_VENV_PYTHON=/opt/otm-venv/bin/python \
    JAVA_MEM=4g \
    PYTHONUNBUFFERED=1

EXPOSE 8080

ENTRYPOINT ["/app/docker-entrypoint.sh"]
