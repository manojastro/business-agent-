"""FastAPI application factory.

API versioning: every route is under /api/v1. Breaking changes get /api/v2 alongside v1 for at
least one release; additive fields may appear in v1 responses at any time.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import errors
from app.api.middleware import GuardMiddleware
from app.api.routes_admin import router as admin_router
from app.api.routes_core import router as core_router
from app.api.routes_investigations import router as inv_router
from app.config import get_settings
from app.telemetry import setup_tracing


def create_app() -> FastAPI:
    s = get_settings()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    app = FastAPI(
        title="Metric Investigator API",
        version="1.0.0",
        description=(
            "Investigates business-metric changes with constrained queries, verified evidence and human review. "
            "All mutations require the session cookie plus the X-CSRF-Token header returned by /auth/session."
        ),
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )
    errors.install(app)
    app.add_middleware(GuardMiddleware, per_minute=s.rate_limit_per_minute, max_body=s.max_request_bytes)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=s.cors_origin_list,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE"],
        allow_headers=["Content-Type", "X-CSRF-Token", "Idempotency-Key", "Last-Event-ID"],
    )
    app.include_router(core_router)
    app.include_router(inv_router)
    app.include_router(admin_router)
    if setup_tracing("metric-investigator-api"):
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor  # pragma: no cover - optional

        FastAPIInstrumentor.instrument_app(app)
    return app


app = create_app()
