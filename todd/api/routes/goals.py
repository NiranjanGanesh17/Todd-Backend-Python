"""GET /v1/goals and POST /v1/goals/waitlist — the locked Pro tab."""

from __future__ import annotations

import uuid

from fastapi import APIRouter
from sqlalchemy import text

from todd.api.deps import SessionDep, SettingsDep, UserIdDep
from todd.schemas.api.models import GoalsLockedResponse, GoalTeaser, WaitlistResponse

router = APIRouter(prefix="/v1/goals", tags=["goals"])


@router.get("", response_model=GoalsLockedResponse, summary="Locked Pro tab with teasers")
async def get_goals(
    session: SessionDep, user_id: UserIdDep, settings: SettingsDep
) -> GoalsLockedResponse:
    on_waitlist = bool(
        (
            await session.execute(
                text("SELECT pro_waitlist_at FROM users WHERE id = :id"),
                {"id": uuid.UUID(user_id)},
            )
        ).scalar_one_or_none()
    )

    # Teasers are generated from the user's own insights. A teaser with no data
    # behind it is hidden rather than invented.
    teasers = [
        GoalTeaser(type=row["type"], title=row["title"], body=row["body"])
        for row in (
            await session.execute(
                text(
                    "SELECT type::text AS type, title, body FROM insights "
                    "WHERE status <> 'dismissed' ORDER BY created_at DESC LIMIT 3"
                )
            )
        )
        .mappings()
        .all()
    ]

    return GoalsLockedResponse(
        mode=settings.pro_lock_mode,
        price_inr=(settings.pro_price_paise / 100) if settings.pro_price_paise else None,
        teasers=teasers,
        on_waitlist=on_waitlist,
    )


@router.post("/waitlist", response_model=WaitlistResponse, summary="Join the Pro waitlist")
async def join_waitlist(session: SessionDep, user_id: UserIdDep) -> WaitlistResponse:
    # Idempotent: joining twice keeps the first timestamp.
    await session.execute(
        text("UPDATE users SET pro_waitlist_at = COALESCE(pro_waitlist_at, now()) WHERE id = :id"),
        {"id": uuid.UUID(user_id)},
    )
    return WaitlistResponse()
