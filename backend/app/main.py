"""Nightingale FastAPI application entrypoint.

Creates the app via `create_app()` (factory) and exposes the module-level
`app` that uvicorn targets: `uvicorn app.main:app --host 0.0.0.0 --port 8000`.
CORS is opened for the SPA; domain routers are registered under `/api`;
`/healthz` is the container healthcheck target and pings the database.
"""

import logging
import traceback
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import api_router
from app.api.routes.health import router as health_router
from app.core.config import get_settings
from app.services.redaction import scrub

logger = logging.getLogger("uvicorn.error")


class PHIScrubbingFilter(logging.Filter):
    """Scrub any PHI pattern from log records before they are emitted.

    Reuses the deterministic redaction pipeline (`app.services.redaction`) so
    even a developer mistake that interpolates a name / IC / phone into a log
    line is neutralized before it is written (docs/SECURITY.md §d.2 — "clean
    logs"). Also scrubs formatted exception tracebacks.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = scrub(record.getMessage())
        record.args = ()
        if record.exc_info:
            record.exc_text = scrub("".join(traceback.format_exception(*record.exc_info)))
            record.exc_info = None
        elif record.exc_text:
            record.exc_text = scrub(record.exc_text)
        return True


def _install_logging_scrubber() -> None:
    """Attach the PHI scrubber to the root + uvicorn loggers."""
    ph_filter = PHIScrubbingFilter()
    logging.getLogger().addFilter(ph_filter)
    logging.getLogger("uvicorn").addFilter(ph_filter)
    logging.getLogger("uvicorn.error").addFilter(ph_filter)
    logging.getLogger("uvicorn.access").addFilter(ph_filter)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Startup/shutdown hook: log the effective runtime configuration."""
    settings = get_settings()
    logger.info(
        "Starting %s (env=%s, llm_mock=%s)",
        settings.app_name,
        settings.app_env,
        settings.llm_mock,
    )
    yield
    logger.info("Shutting down %s", settings.app_name)


def create_app() -> FastAPI:
    """Application factory: assemble middleware and routers."""
    settings = get_settings()
    _install_logging_scrubber()

    app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)

    # CORS for the SPA (Vite dev server on 5173). Origins come from config.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Root health probe (container healthcheck target) — pings the DB.
    app.include_router(health_router)

    # Versioned domain API surface. Domain routers register in api_router.
    app.include_router(api_router, prefix="/api")

    return app


app = create_app()
