# Homestead Digital Twin core service.
#
# One image serves both node roles (SDD section 16.1): the primary node in the
# power container and the physically separate secondary control node. Role is
# chosen at runtime through HOMESTEAD_NODE_ROLE, never by building a second
# image -- a divergent secondary image is a secondary node you cannot trust.
#
# Build from the repository root:
#   docker build -f deploy/Dockerfile -t homestead-twin:local .

# --- Stage 1: build a self-contained virtualenv ----------------------------
FROM python:3.11-slim-bookworm AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

# psycopg[binary] ships wheels, so no libpq build chain is needed here. gcc is
# kept out of the final image regardless.
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /build

# Dependency metadata first so layer caching survives source edits.
COPY pyproject.toml README.md ./
COPY src/ ./src/

RUN pip install --upgrade pip setuptools wheel \
    && pip install ".[postgres]"

# --- Stage 2: runtime ------------------------------------------------------
FROM python:3.11-slim-bookworm AS runtime

LABEL org.opencontainers.image.title="homestead-twin" \
      org.opencontainers.image.description="Homestead Digital Twin and Master Control Platform" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.source="https://github.com/ethanbissbort/homestead-twin"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    HOMESTEAD_API_HOST=0.0.0.0 \
    HOMESTEAD_API_PORT=8000

# curl is the healthcheck client; tini reaps the uvicorn worker so a stopped
# container does not leave the MQTT session hanging until keepalive expiry.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl tini \
    && rm -rf /var/lib/apt/lists/*

# Non-root from here on. The platform can command physical plant; it has no
# business running as uid 0 (SDD section 15.2, least privilege).
RUN groupadd --gid 10001 homestead \
    && useradd --uid 10001 --gid homestead --create-home --shell /usr/sbin/nologin homestead

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app

# The machine-readable design package and the validator travel with the image so
# that `homestead-twin load-all` and `homestead-twin validate` work offline on a
# node with no repository checkout.
COPY --chown=homestead:homestead data/ /app/data/
COPY --chown=homestead:homestead schemas/ /app/schemas/
COPY --chown=homestead:homestead tools/ /app/tools/
COPY --chown=homestead:homestead src/ /app/src/
COPY --chown=homestead:homestead pyproject.toml README.md /app/

# var/ holds the SQLite fallback database and local backups.
RUN mkdir -p /app/var/backups && chown -R homestead:homestead /app/var

USER homestead

# HOMESTEAD_DATA_DIR/SCHEMA_DIR default to <package root>/../../data, which
# resolves to /app/data because the source tree is copied in.
ENV HOMESTEAD_DATA_DIR=/app/data \
    HOMESTEAD_SCHEMA_DIR=/app/schemas \
    HOMESTEAD_DATABASE_URL=sqlite:////app/var/homestead.db

EXPOSE 8000
VOLUME ["/app/var"]

# /health is unauthenticated by design: it reports liveness, version and node
# role only. It never exposes telemetry, credentials or control state.
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${HOMESTEAD_API_PORT}/health" || exit 1

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["homestead-twin", "serve"]
