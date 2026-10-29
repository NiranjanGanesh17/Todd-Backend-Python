"""Request-scoped dependencies: settings, the current user, a scoped session.

Every route that touches user data depends on `scoped_session`, which opens a
transaction, downgrades to `app_user` and sets `app.user_id`. A route that
forgets gets no rows rather than everyone's — RLS is forced, and an unset
`app.user_id` matches no policy.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Annotated

import jwt
from fastapi import Depends, Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from todd.api.errors import ApiError
from todd.config import Settings, get_settings
from todd.db.session import get_sessionmaker, with_user
from todd.log import get_logger

logger = get_logger(__name__)

SettingsDep = Annotated[Settings, Depends(get_settings)]


def _bearer(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    return token if scheme.lower() == "bearer" and token else None


async def current_user_id(request: Request, settings: SettingsDep) -> str:
    """The caller's user id, from a Supabase JWT or the dev bypass.

    `AUTH_DEV_BYPASS` cannot be true in production — config refuses to load
    (§8.17) — so this branch is unreachable there.
    """
    if settings.auth_dev_bypass:
        return settings.auth_dev_user_id

    token = _bearer(request)
    if not token:
        raise ApiError(401, "unauthorized", "Sign in to continue.")
    if not settings.supabase_jwt_secret:
        raise ApiError(503, "unavailable", "Authentication is not configured.")
    try:
        claims = jwt.decode(
            token,
            settings.supabase_jwt_secret,
            algorithms=["HS256"],
            audience="authenticated",
        )
    except jwt.PyJWTError as exc:
        logger.info("jwt_rejected", reason=type(exc).__name__)
        raise ApiError(401, "unauthorized", "Your session has expired.") from exc

    subject = claims.get("sub")
    if not subject:
        raise ApiError(401, "unauthorized", "That token carries no subject.")
    return str(subject)


UserIdDep = Annotated[str, Depends(current_user_id)]


async def ensure_dev_user(session: AsyncSession, settings: Settings) -> None:
    """Make the bypass user real.

    Without a `users` row the dev user owns nothing and every screen 404s. Runs
    as the connection owner, before the session is downgraded to `app_user`.
    """
    await session.execute(
        text(
            "INSERT INTO users (id, email, display_name) "
            "VALUES (:id, :email, :name) ON CONFLICT (id) DO NOTHING"
        ),
        {
            "id": uuid.UUID(settings.auth_dev_user_id),
            "email": settings.auth_dev_user_email,
            "name": settings.auth_dev_user_email.split("@")[0].title(),
        },
    )


async def scoped_session(user_id: UserIdDep, settings: SettingsDep) -> AsyncIterator[AsyncSession]:
    """A session already scoped to the caller. The transaction commits on a
    clean return and rolls back on any exception."""
    async with get_sessionmaker()() as session, session.begin():
        if settings.auth_dev_bypass:
            await ensure_dev_user(session, settings)
        async with with_user(session, user_id) as scoped:
            yield scoped


SessionDep = Annotated[AsyncSession, Depends(scoped_session)]
