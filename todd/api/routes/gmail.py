"""Gmail connect, OAuth callback, status, sync and disconnect.

Only `/v1/gmail/status` is built: the web app polls it on /connecting, and it
answers truthfully from the database. The rest are step 8 and return 501 rather
than a plausible-looking lie — the web app shows its own error state.
"""

from __future__ import annotations

from fastapi import APIRouter

from todd.api.deps import SessionDep, UserIdDep
from todd.api.errors import ApiError
from todd.schemas.api.models import (
    GmailBackfill,
    GmailConnection,
    GmailConnectResponse,
    GmailStatusResponse,
)

router = APIRouter(tags=["gmail"])


def _iso(value: object) -> str | None:
    return value.isoformat() if hasattr(value, "isoformat") else None


@router.get(
    "/v1/gmail/status",
    response_model=GmailStatusResponse,
    summary="Connection plus live backfill progress",
)
async def gmail_status(session: SessionDep, user_id: UserIdDep) -> GmailStatusResponse:
    from sqlalchemy import text

    conn_row = (
        (
            await session.execute(
                text(
                    "SELECT id, google_email, status::text AS status, scopes, last_synced_at, "
                    "       watch_expires_at, created_at FROM email_connections "
                    "ORDER BY created_at DESC LIMIT 1"
                )
            )
        )
        .mappings()
        .one_or_none()
    )

    connection = (
        None
        if conn_row is None
        else GmailConnection(
            id=str(conn_row["id"]),
            google_email=conn_row["google_email"],
            status=conn_row["status"],
            scopes=list(conn_row["scopes"] or []),
            last_synced_at=_iso(conn_row["last_synced_at"]),
            watch_expires_at=_iso(conn_row["watch_expires_at"]),
            created_at=_iso(conn_row["created_at"]) or "",
        )
    )

    run_row = (
        (
            await session.execute(
                text(
                    "SELECT state, messages_found, messages_processed, statements_found, "
                    "       alerts_found, started_at, finished_at FROM backfill_runs "
                    "ORDER BY created_at DESC LIMIT 1"
                )
            )
        )
        .mappings()
        .one_or_none()
    )

    backfill = (
        None
        if run_row is None
        else GmailBackfill(
            state=run_row["state"],
            messages_found=run_row["messages_found"],
            messages_processed=run_row["messages_processed"],
            statements_found=run_row["statements_found"],
            alerts_found=run_row["alerts_found"],
            started_at=_iso(run_row["started_at"]),
            finished_at=_iso(run_row["finished_at"]),
        )
    )

    return GmailStatusResponse(connection=connection, backfill=backfill)


_NOT_YET = "Gmail ingest is not built yet. Upload a statement PDF instead."


@router.post(
    "/v1/sources/gmail/connect",
    response_model=GmailConnectResponse,
    summary="Begin consent, returns {authUrl}",
)
async def gmail_connect() -> GmailConnectResponse:
    # The web app POSTs this with no body. FastAPI needs no body model here, so
    # an empty POST cannot 422 (§6).
    raise ApiError(501, "not_implemented", _NOT_YET)


@router.get("/v1/gmail/callback", summary="OAuth redirect target")
async def gmail_callback() -> None:
    raise ApiError(501, "not_implemented", _NOT_YET)


@router.post("/v1/gmail/sync", summary="Catch-up sync")
async def gmail_sync() -> None:
    raise ApiError(501, "not_implemented", _NOT_YET)


@router.delete("/v1/gmail", summary="Disconnect, revoke token, stop watch")
async def gmail_disconnect() -> None:
    raise ApiError(501, "not_implemented", _NOT_YET)
