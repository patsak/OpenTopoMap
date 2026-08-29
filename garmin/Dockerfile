# OpenTopoMap Garmin map service
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

COPY requirements-server.txt /app/requirements-server.txt
RUN python3 -m venv --system-site-packages /opt/otm-venv \
    && /opt/otm-venv/bin/pip install --no-cache-dir -U pip \
    && /opt/otm-venv/bin/pip install --no-cache-dir -r /app/requirements-server.txt

COPY . /app

RUN mkdir -p /app/tools \
    && if [ ! -f /app/tools/mkgmap.jar ]; then \
         jar="$(find /app/tools -name mkgmap.jar 2>/dev/null | head -1)"; \
         if [ -n "$jar" ]; then ln -sfn "$jar" /app/tools/mkgmap.jar; \
         else \
           curl --retry 5 --retry-all-errors -fsSL -o /tmp/mkgmap.zip \
             "https://www.mkgmap.org.uk/download/${MKGMAP_VERSION}.zip" \
           && unzip -q /tmp/mkgmap.zip -d /app/tools \
           && ln -sfn "/app/tools/${MKGMAP_VERSION}/mkgmap.jar" /app/tools/mkgmap.jar \
           && rm -f /tmp/mkgmap.zip; \
         fi; \
       fi \
    && if ! find /app/tools -name splitter.jar 2>/dev/null | grep -q .; then \
           curl --retry 5 --retry-all-errors -fsSL -o /tmp/splitter.zip \
             "https://www.mkgmap.org.uk/download/${SPLITTER_VERSION}.zip" \
           && unzip -q /tmp/splitter.zip -d /app/tools \
           && rm -f /tmp/splitter.zip; \
       fi \
    && ln -sfn "$(find /app/tools -name splitter.jar | head -1)" /app/tools/splitter.jar \
    && test -f /app/tools/mkgmap.jar \
    && test -e /app/tools/splitter.jar \
    && chmod +x /app/docker-entrypoint.sh

ENV PATH="/opt/otm-venv/bin:${PATH}" \
    OTM_HOST=0.0.0.0 \
    OTM_PORT=8080 \
    OTM_DOCKER=1 \
    OTM_VENV_PYTHON=/opt/otm-venv/bin/python \
    JAVA_MEM=4g \
    PYTHONUNBUFFERED=1

EXPOSE 8080

ENTRYPOINT ["/app/docker-entrypoint.sh"]
