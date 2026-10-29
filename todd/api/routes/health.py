"""Liveness and readiness.

`/healthz` answers from the process alone: if it cannot reply, the process is
gone. `/readyz` reports whether the dependencies this instance needs are
reachable, and returns 503 when they are not, so a rollout does not send
traffic to an instance that cannot serve it.

Both answer in the shape `Todd-Frontend/contracts/api.ts` declares, which is
why a liveness check still carries a `checks` object — with both marked
`skipped`, because it deliberately does not look.
"""

from __future__ import annotations

import time

from fastapi import APIRouter, Response
from sqlalchemy import text

from todd.api.deps import SettingsDep
from todd.db.session import get_sessionmaker
from todd.log import get_logger
from todd.schemas.api.models import CheckState, HealthChecks, HealthResponse

logger = get_logger(__name__)
router = APIRouter(tags=["health"])

_STARTED = time.monotonic()


def _uptime() -> float:
    return round(time.monotonic() - _STARTED, 3)


@router.get("/healthz", response_model=HealthResponse, summary="Liveness")
async def healthz(settings: SettingsDep) -> HealthResponse:
    return HealthResponse(
        service=settings.service_name,
        version=settings.version,
        uptime_seconds=_uptime(),
        checks=HealthChecks(database="skipped", redis="skipped"),
    )


async def _database_state() -> CheckState:
    try:
        async with get_sessionmaker()() as session:
            await session.execute(text("SELECT 1"))
    except Exception as exc:
        logger.warning("readyz_database_down", error=str(exc))
        return "down"
    return "ok"


async def _redis_state(settings: SettingsDep) -> CheckState:
    try:
        import redis.asyncio as redis
    except ImportError:
        return "skipped"
    client = redis.from_url(settings.redis_url)  # type: ignore[no-untyped-call]
    try:
        await client.ping()
    except Exception as exc:
        logger.warning("readyz_redis_down", error=str(exc))
        return "down"
    finally:
        await client.aclose()
    return "ok"


@router.get("/readyz", response_model=HealthResponse, summary="Readiness")
async def readyz(settings: SettingsDep, response: Response) -> HealthResponse:
    checks = HealthChecks(database=await _database_state(), redis=await _redis_state(settings))
    if "down" in (checks.database, checks.redis):
        response.status_code = 503
    return HealthResponse(
        service=settings.service_name,
        version=settings.version,
        uptime_seconds=_uptime(),
        checks=checks,
    )
