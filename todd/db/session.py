"""Engine, session factory, and the RLS session scope.

`with_user()` is the only sanctioned way to touch a user-scoped table. It
downgrades the connection to the low-privilege `app_user` role and sets
`app.user_id` for the life of the transaction, which is what every RLS policy
reads.

`SET LOCAL` is transaction-scoped. Outside a transaction it is a no-op and the
setting leaks between requests on a pooled connection, so `with_user()` opens
one itself if the caller has not. The cross-tenant test in `tests/test_rls.py`
is what proves this still holds.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from todd.config import Settings

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def init_engine(settings: Settings) -> AsyncEngine:
    global _engine, _sessionmaker
    if _engine is None:
        _engine = create_async_engine(
            settings.database_url,
            pool_size=settings.database_pool_max,
            max_overflow=0,
            pool_pre_ping=True,
            echo=False,
            connect_args={"sslmode": "require"} if settings.database_ssl else {},
        )
        _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


def get_engine() -> AsyncEngine:
    if _engine is None:
        raise RuntimeError("init_engine() has not run. Call it from the app lifespan.")
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    if _sessionmaker is None:
        raise RuntimeError("init_engine() has not run. Call it from the app lifespan.")
    return _sessionmaker


async def dispose_engine() -> None:
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None


@asynccontextmanager
async def with_user(session: AsyncSession, user_id: str) -> AsyncIterator[AsyncSession]:
    """Scope every statement in the block to one tenant.

    Both settings are `LOCAL`, so they unwind with the transaction and cannot
    survive the connection going back to the pool.
    """
    if not session.in_transaction():
        async with session.begin():
            await _apply_scope(session, user_id)
            yield session
        return
    await _apply_scope(session, user_id)
    yield session


async def _apply_scope(session: AsyncSession, user_id: str) -> None:
    await session.execute(text("SET LOCAL ROLE app_user"))
    await session.execute(
        text("SELECT set_config('app.user_id', :uid, true)"), {"uid": str(user_id)}
    )


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """An unscoped session — migrations, seeds and the readiness probe only."""
    async with get_sessionmaker()() as session:
        yield session
