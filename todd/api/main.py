"""App factory: middleware, router registration, lifespan.

`create_app()` takes settings rather than reading them, so a test can build an
app against a different database without touching the environment.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

from todd.api.errors import install_error_handlers, request_id_middleware
from todd.api.routes import documents, gmail, goals, health, screens
from todd.config import Settings, get_settings
from todd.db.session import dispose_engine, init_engine
from todd.log import configure_logging, get_logger

logger = get_logger(__name__)

#: `@fastify/helmet`'s equivalent. The API serves JSON to one known origin, so
#: the policy can be far stricter than a page's would be.
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
    "Cross-Origin-Resource-Policy": "same-site",
}


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp, *, hsts: bool) -> None:
        super().__init__(app)
        self.hsts = hsts

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)
        for key, value in SECURITY_HEADERS.items():
            response.headers.setdefault(key, value)
        # Only over TLS: sent on plain http://localhost it would pin the
        # browser to https for the whole of localhost.
        if self.hsts:
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )
        return response


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        init_engine(settings)
        logger.info(
            "api_started",
            env=settings.app_env,
            port=settings.port,
            cors_origins=settings.cors_origins,
            auth_dev_bypass=settings.auth_dev_bypass,
        )
        if settings.auth_dev_bypass:
            logger.warning(
                "auth_dev_bypass_enabled",
                detail="Every request is the dev user. Never reachable in production.",
                user_id=settings.auth_dev_user_id,
            )
        yield
        await dispose_engine()

    app = FastAPI(
        title="Todd API",
        version=settings.version,
        summary="Bank statements in Gmail, turned into one combined spend report.",
        lifespan=lifespan,
        docs_url="/docs",
        openapi_url="/openapi.json",
    )

    # Credentials are sent by the web app (`credentials: 'include'`), which the
    # browser refuses to pair with a wildcard origin — so this is an explicit
    # list, from WEB_ORIGIN.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["authorization", "content-type", "x-request-id"],
        expose_headers=["x-request-id"],
        max_age=600,
    )
    app.add_middleware(SecurityHeadersMiddleware, hsts=settings.is_production)
    app.middleware("http")(request_id_middleware)

    # Routes resolve settings through `Depends(get_settings)`, which is cached
    # globally. Without this override the `settings` argument would shape only
    # logging and CORS while every route read the process-wide object — so a
    # test could not build an app with different configuration, and would
    # silently exercise whatever `.env` happened to contain.
    app.dependency_overrides[get_settings] = lambda: settings

    install_error_handlers(app)

    app.include_router(health.router)
    app.include_router(screens.router)
    app.include_router(goals.router)
    app.include_router(gmail.router)
    app.include_router(documents.router)

    return app


app = create_app()
