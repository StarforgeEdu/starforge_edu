# syntax=docker/dockerfile:1.7
# Multi-stage build: builder produces a locked virtual environment; runtime
# installs it outside /app so the development source bind mount cannot hide it.

ARG VCS_REF=unknown

FROM python:3.13-slim@sha256:eb43ff125d8d58d7449dcba7d336c23bcac412f526d861db493b9994d8010280 AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/opt/starforge-venv

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libpq-dev \
        libmagic1 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.11@sha256:0f36cb9361a3346885ca3677e3767016687b5a170c1a6b88465ec14aefec90aa /uv /uvx /usr/local/bin/

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project --no-editable


FROM python:3.13-slim@sha256:eb43ff125d8d58d7449dcba7d336c23bcac412f526d861db493b9994d8010280 AS runtime

ARG VCS_REF

LABEL org.opencontainers.image.title="StarForge Edu" \
      org.opencontainers.image.source="https://github.com/StarforgeEdu/starforge_edu" \
      org.opencontainers.image.revision="${VCS_REF}"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/starforge-venv/bin:$PATH" \
    DJANGO_SETTINGS_MODULE=config.settings.production \
    STARFORGE_IMAGE_REVISION="${VCS_REF}" \
    AWS_EC2_METADATA_DISABLED=true

# WeasyPrint needs the Pango/Cairo stack, font discovery, and raster codecs.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq5 \
        libmagic1 \
        libcairo2 \
        libpango-1.0-0 \
        libpangoft2-1.0-0 \
        libharfbuzz-subset0 \
        libjpeg62-turbo \
        libopenjp2-7 \
        poppler-utils \
        shared-mime-info \
        util-linux \
        fonts-dejavu-core \
        curl \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -r app && useradd -r -g app app

WORKDIR /app
COPY --from=builder /opt/starforge-venv /opt/starforge-venv
COPY --chown=app:app . /app

# TD-17: wire the entrypoint so `docker compose run web <verb>` dispatches
# (web|daphne|worker|beat|migrate|shell). `migrate` runs shared + tenant.
RUN chmod +x /app/docker/entrypoint.sh

USER app
EXPOSE 8000
ENTRYPOINT ["/app/docker/entrypoint.sh"]
CMD ["web"]
