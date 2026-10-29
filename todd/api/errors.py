"""One error envelope for the whole API.

    {"error": {"code": "...", "message": "...", "requestId": "..."}}

The web app reads `error.code` and `error.message` off exactly this shape. A
driver message or a stack trace never reaches it: unhandled exceptions are
logged in full and answered with a generic 500.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from todd.log import get_logger

logger = get_logger(__name__)

REQUEST_ID_HEADER = "x-request-id"

#: Default code per status, used when a raiser does not name one.
_CODES: dict[int, str] = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    409: "conflict",
    413: "payload_too_large",
    422: "validation_failed",
    429: "rate_limited",
    501: "not_implemented",
    503: "unavailable",
}


class ApiError(Exception):
    """Raise this rather than HTTPException when the code matters to the client."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        details: Any = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details


def request_id(request: Request) -> str:
    existing = getattr(request.state, "request_id", None)
    if isinstance(existing, str):
        return existing
    generated = request.headers.get(REQUEST_ID_HEADER) or uuid.uuid4().hex
    request.state.request_id = generated
    return generated


def error_response(
    request: Request,
    status_code: int,
    code: str,
    message: str,
    details: Any = None,
) -> JSONResponse:
    body: dict[str, Any] = {"code": code, "message": message, "requestId": request_id(request)}
    if details is not None:
        body["details"] = details
    return JSONResponse(
        status_code=status_code,
        content={"error": body},
        headers={REQUEST_ID_HEADER: body["requestId"]},
    )


async def request_id_middleware(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    rid = request_id(request)
    structlog_bind(request_id=rid, path=request.url.path, method=request.method)
    response = await call_next(request)
    response.headers[REQUEST_ID_HEADER] = rid
    return response


def structlog_bind(**kwargs: Any) -> None:
    import structlog

    structlog.contextvars.bind_contextvars(**kwargs)


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError) -> JSONResponse:
        return error_response(request, exc.status_code, exc.code, exc.message, exc.details)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = _CODES.get(exc.status_code, "error")
        detail = exc.detail if isinstance(exc.detail, str) else code
        return error_response(request, exc.status_code, code, detail)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        return error_response(
            request,
            422,
            "validation_failed",
            "The request did not match the expected shape.",
            details=exc.errors(),
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        # Full detail to the log, nothing but a request id to the client.
        logger.exception(
            "unhandled_exception",
            path=request.url.path,
            method=request.method,
            request_id=request_id(request),
            exc_type=type(exc).__name__,
        )
        return error_response(request, 500, "internal_error", "Something went wrong.")
