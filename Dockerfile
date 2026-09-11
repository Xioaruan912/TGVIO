# syntax=docker/dockerfile:1.7

# Pin the complete multi-platform index. HostDZire selects the linux/amd64
# manifest from this immutable digest (Python 3.11.16, Debian trixie-slim).
ARG PYTHON_IMAGE=python:3.11-slim@sha256:9534e5a8e315485d4061ed659af0fd78a284c015f9b73661b41d6bab25604534

FROM ${PYTHON_IMAGE} AS dependencies
WORKDIR /build
COPY requirements.lock ./requirements.lock
RUN python -m pip install \
      --disable-pip-version-check \
      --no-cache-dir \
      --no-build-isolation \
      --no-deps \
      --require-hashes \
      --target /opt/tgvio/site-packages \
      -r requirements.lock \
    && find /opt/tgvio/site-packages -type d -name __pycache__ -prune -exec rm -rf '{}' +

FROM ${PYTHON_IMAGE} AS runtime-base
WORKDIR /app
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=dependencies /opt/tgvio/site-packages /opt/tgvio/site-packages
ENV PYTHONPATH=/app/src:/opt/tgvio/site-packages \
    PATH=/usr/local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

ARG APP_COMMIT=unknown
ARG RELEASE_ID=unreleased
ARG SOURCE_MANIFEST=unknown
ARG REQUIREMENTS_LOCK_SHA256=unknown
ENV APP_COMMIT=${APP_COMMIT}
LABEL org.opencontainers.image.revision=${APP_COMMIT} \
      org.opencontainers.image.version=${RELEASE_ID} \
      io.tgvio.source-manifest=${SOURCE_MANIFEST} \
      io.tgvio.requirements-lock-sha256=${REQUIREMENTS_LOCK_SHA256}

FROM runtime-base AS test
COPY .dockerignore Dockerfile docker-compose.yml requirements.txt requirements.lock ./
COPY deploy ./deploy
COPY src ./src
COPY tests ./tests
COPY scripts ./scripts
CMD ["sh", "scripts/check_foundation.sh"]

FROM runtime-base AS runtime
COPY src ./src
COPY scripts/healthcheck.py scripts/image_inspect.py ./scripts/
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "scripts/healthcheck.py"]
CMD ["python", "-m", "tgvio.main"]
