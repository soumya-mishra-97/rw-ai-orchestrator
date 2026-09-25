# syntax=docker/dockerfile:1
# Production image: one process serves the web UI, the API and the pipeline.
FROM python:3.13-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    SDLC_DATA_DIR=/data \
    SDLC_MODE=auto

COPY --from=ghcr.io/astral-sh/uv:0.12 /uv /usr/local/bin/uv
WORKDIR /app

# Dependencies first so they cache independently of source changes.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
COPY scenarios ./scenarios
RUN uv sync --frozen --no-dev

RUN useradd --create-home --uid 10001 app && mkdir -p /data && chown app /data
USER app
VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=15s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz')" || exit 1

# Single worker on purpose: runs execute in-process and state lives in SQLite.
CMD ["/app/.venv/bin/sdlc", "serve", "--host", "0.0.0.0", "--port", "8000"]
