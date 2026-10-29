"""structlog setup: JSON in production, a console renderer in development.

Named `log` rather than `logging` so nothing has to think about whether an
`import logging` inside this package resolves to the standard library.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog

from todd.config import Settings

#: Keys whose values must never reach a log line. Account numbers are last-4
#: everywhere, and a statement password exists only for the length of a parse.
REDACTED_KEYS = frozenset(
    {
        "password",
        "pdf_password",
        "refresh_token",
        "access_token",
        "authorization",
        "api_key",
        "anthropic_api_key",
        "service_role_key",
        "encryption_master_key",
        "account_number",
        "full_account_number",
    }
)


def _scrub(
    _logger: Any, _method: str, event_dict: structlog.types.EventDict
) -> structlog.types.EventDict:
    """Last line of defence. A secret reaches a log through a careless kwarg far
    more often than through a deliberate one, so filter on the way out."""
    for key in list(event_dict):
        if key.lower() in REDACTED_KEYS:
            event_dict[key] = "[redacted]"
    return event_dict


def configure_logging(settings: Settings) -> None:
    level = getattr(logging, settings.log_level.upper(), logging.INFO)

    shared: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        _scrub,
    ]

    renderer: structlog.types.Processor = (
        structlog.processors.JSONRenderer()
        if settings.is_production
        else structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())
    )

    structlog.configure(
        processors=[*shared, structlog.processors.format_exc_info, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )

    # Uvicorn and SQLAlchemy log through the standard library; send them to the
    # same place so production gets one stream of JSON, not two formats.
    logging.basicConfig(format="%(message)s", stream=sys.stderr, level=level, force=True)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "sqlalchemy.engine"):
        logging.getLogger(name).handlers.clear()
        logging.getLogger(name).propagate = True


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)  # type: ignore[no-any-return]
