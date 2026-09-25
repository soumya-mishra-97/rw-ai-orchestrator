"""ASGI entry point.

    uvicorn sdlc_loop.api.main:app_factory --factory            # what `sdlc serve` runs
    uvicorn sdlc_loop.api.main:app_factory --factory --reload   # development (`make dev`)

The factory loads ``.env``, picks live or demo mode (``SDLC_MODE``), wires the
container and returns the FastAPI app, which also serves the web UI.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI

from sdlc_loop.api.app import create_app
from sdlc_loop.config import Settings
from sdlc_loop.logging_setup import configure_logging
from sdlc_loop.runtime import load_environment, resolve_runtime
from sdlc_loop.services import build_container

log = logging.getLogger(__name__)


def build_app(settings: Settings | None = None, *, mode: str | None = None) -> FastAPI:
    load_environment()
    settings = settings or Settings()
    configure_logging(settings.log_level, json_output=settings.log_json)
    runtime = resolve_runtime(settings, requested=mode)
    log.info("starting in %s mode: %s", runtime.mode.value, runtime.reason)
    container = build_container(settings, llm=runtime.llm)
    return create_app(container, demo=runtime.demo, mode_note=runtime.reason)


def app_factory() -> FastAPI:
    return build_app()
