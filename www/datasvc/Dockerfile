# OpenTopoMap data service: the region extracts and what is built from them.
# Two entry points out of one image: the nightly job on a supercronic schedule
# (the Geofabrik sync with osmium, then the Overpass GOL with gol, then the
# hiking router's graphs with GRASS - www/routing), and the preview worker
# (tilemaker) that renders one drawn bbox at a time - see the datasvc-job and
# datasvc-preview services.

# GeoDesk's gol tool, for the job's GOL step (otmlib.gol, read by
# www/overpass). Compiled here rather than taken from the release: the released
# binary is x86_64 only and fails under qemu on Apple Silicon. Built on the
# same base as the image below, so it links against exactly the libraries that
# are there.
FROM debian:13-slim AS gol

ARG GOL_VERSION=2.5.3
# gol's CMakeLists fetches libgeodesk from its main branch and cpp-httplib
# without a tag, so each is pinned here and handed to CMake as an already
# fetched source: libgeodesk at its last commit before the gol 2.5.3 release,
# cpp-httplib at a release from before it. 2.5 is the first with `gol update`
# (otmlib.gol.update). zlib and gtl, which CMake still
# fetches, are pinned to a release upstream.
ARG LIBGEODESK_COMMIT=7b3465b0fc9af10337299bf1a0dbcaa444d07173
ARG CPPHTTPLIB_TAG=v0.63.0

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
        cmake \
        git \
        libssl-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /src
RUN git clone --depth 1 --branch "v${GOL_VERSION}" https://github.com/clarisma/geodesk-gol.git gol \
    && git init -q libgeodesk \
    && git -C libgeodesk fetch -q --depth 1 https://github.com/clarisma/libgeodesk.git "${LIBGEODESK_COMMIT}" \
    && git -C libgeodesk checkout -q FETCH_HEAD \
    && git clone --depth 1 --branch "${CPPHTTPLIB_TAG}" https://github.com/yhirose/cpp-httplib.git cpphttplib

RUN cmake -S gol -B build \
        -DCMAKE_BUILD_TYPE=Release \
        -DGEODESK_GOL_VERSION="${GOL_VERSION}" \
        -DFETCHCONTENT_SOURCE_DIR_GEODESK=/src/libgeodesk \
        -DFETCHCONTENT_SOURCE_DIR_CPPHTTPLIB=/src/cpphttplib \
    && cmake --build build --target gol -j"$(nproc)" \
    && ./build/gol --version


FROM debian:13-slim

ENV DEBIAN_FRONTEND=noninteractive

# The five tilemaker runtime libraries below are what its binary is dynamically
# linked against; upstream builds it on debian:trixie-slim (since
# systemed/tilemaker#940; before that bookworm, which this image followed),
# the same base as this image, so the copied binary finds exactly these
# sonames. Upstream moving its base again shows up as a missing .so at the
# `tilemaker --help` check at the end of this file; then run
#   docker run --rm --entrypoint sh ghcr.io/systemed/tilemaker:master \
#     -c "cat /etc/debian_version; ldd /usr/src/app/tilemaker"
# and follow it here.
# grass-core, python3-gdal and python3-numpy are the routing graph build's:
# r.watershed (GRASS 8.4 here), the DEM reads, and the numpy those GDAL
# bindings were built against.
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 \
        python3-pip \
        python3-venv \
        python3-gdal \
        python3-numpy \
        grass-core \
        osmium-tool \
        gdal-bin \
        curl \
        ca-certificates \
        libpq5 \
        libboost-filesystem1.83.0 \
        libboost-program-options1.83.0 \
        liblua5.1-0 \
        libshp4 \
        libsqlite3-0 \
        libssl3t64 \
    && rm -rf /var/lib/apt/lists/*

# gol, from the stage at the top; libssl3t64 above is what it links against
# besides libc and libstdc++.
COPY --from=gol /src/build/gol /usr/local/bin/gol

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

COPY www/datasvc/requirements.txt /app/requirements.txt
COPY www/routing/requirements.txt /app/routing-requirements.txt
# The routing build's packages with numpy held at Debian's: a newer scipy or
# numba would pull numpy 2 into the venv, and the GDAL bindings, built
# against Debian's numpy, would fail to import.
RUN python3 -m venv --system-site-packages /opt/otm-venv \
    && /opt/otm-venv/bin/pip install --no-cache-dir -U pip \
    && /opt/otm-venv/bin/pip install --no-cache-dir -r /app/requirements.txt \
    && python3 -c "import numpy; print(f'numpy=={numpy.__version__}')" > /tmp/numpy-pin.txt \
    && /opt/otm-venv/bin/pip install --no-cache-dir -c /tmp/numpy-pin.txt -r /app/routing-requirements.txt

COPY www/datasvc /app/datasvc
COPY www/otmlib /app/otmlib
COPY www/routing /app/routing
COPY vector/tilemaker /app/vector/tilemaker
RUN cp /app/datasvc/crontab /app/crontab \
    && tilemaker --help > /dev/null \
    && gol --version \
    && grass --config python_path > /dev/null \
    && /opt/otm-venv/bin/python -c "import osgeo.gdal, numba, scipy, routing.build.pipeline, routing.compile"

# NUMBA_CPU_NAME=generic: numba compiles the routing build's loops for the
# baseline of the architecture, not for what the host CPU claims. An Apple M4
# under Colima lists SVE2 (from SME) that it does not run outside streaming
# mode: compiled for the host, those loops die with "Illegal instruction".
ENV PATH="/opt/otm-venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    OTM_DATA_DIR=/app/data \
    OTM_DATASVC_CONFIG=/app/datasvc/config.yaml \
    OTM_ROUTING_CONFIG=/app/routing/config.yaml \
    NUMBA_CACHE_DIR=/tmp/numba-cache \
    NUMBA_CPU_NAME=generic \
    DATABASE_URL=postgresql://otm:otm@postgres:5432/otm

CMD ["supercronic", "/app/crontab"]
