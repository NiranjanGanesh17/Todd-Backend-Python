"""Gmail connect, OAuth callback, status, sync and disconnect.

The scope is `gmail.readonly`, the search is built from the sender allowlist,
and the refresh token is stored with envelope encryption. The backfill is
fire-and-forget: the redirect returns before any mail is read, and the browser
lands on `/connecting`, which polls `/v1/gmail/status` (§7.5, §8.14).
"""

from __future__ import annotations

import datetime as dt
import secrets
import uuid

from fastapi import APIRouter, BackgroundTasks, Query
from fastapi.responses import RedirectResponse
from sqlalchemy import text

from todd.api.deps import SessionDep, SettingsDep, UserIdDep
from todd.api.errors import ApiError
from todd.db.session import get_sessionmaker, with_user
from todd.log import get_logger
from todd.schemas.api.models import (
    GmailBackfill,
    GmailConnection,
    GmailConnectResponse,
    GmailStatusResponse,
)
from todd.worker.agents.inbox import run_backfill
from todd.worker.lib.crypto import seal
from todd.worker.lib.gmail import SCOPES, GmailClient, build_consent_url, exchange_code

logger = get_logger(__name__)
router = APIRouter(tags=["gmail"])

#: A CSRF state is single use and short lived. Ten minutes is longer than any
#: honest consent screen takes.
STATE_TTL = dt.timedelta(minutes=10)


def _iso(value: object) -> str | None:
    return value.isoformat() if hasattr(value, "isoformat") else None


def _require_google(settings: SettingsDep) -> None:
    if not settings.google_client_id or not settings.google_client_secret:
        raise ApiError(503, "unavailable", "Gmail is not configured on this server.")


# ------------------------------------------------------------------- status


@router.get(
    "/v1/gmail/status",
    response_model=GmailStatusResponse,
    summary="Connection plus live backfill progress",
)
async def gmail_status(session: SessionDep, user_id: UserIdDep) -> GmailStatusResponse:
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


# ------------------------------------------------------------------ connect


@router.post(
    "/v1/sources/gmail/connect",
    response_model=GmailConnectResponse,
    summary="Begin consent, returns {authUrl}",
)
async def gmail_connect(
    session: SessionDep, user_id: UserIdDep, settings: SettingsDep
) -> GmailConnectResponse:
    """Start consent. The web app POSTs this with **no body**, so no body model
    is declared here — one would make an empty POST a 422 (§6)."""
    _require_google(settings)

    state = secrets.token_urlsafe(32)
    auth_url, code_verifier = build_consent_url(settings, state)

    # The PKCE verifier is stored with the state: the callback runs on a
    # different Flow instance and Google rejects the exchange without it.
    await session.execute(
        text(
            "INSERT INTO oauth_states (state, user_id, return_path, code_verifier, "
            "  expires_at) "
            "VALUES (:state, :uid, '/connecting', :verifier, :exp)"
        ),
        {
            "state": state,
            "uid": uuid.UUID(user_id),
            "verifier": code_verifier,
            "exp": dt.datetime.now(tz=dt.UTC) + STATE_TTL,
        },
    )
    return GmailConnectResponse(auth_url=auth_url)


@router.get("/v1/gmail/callback", summary="OAuth redirect target")
async def gmail_callback(
    settings: SettingsDep,
    background: BackgroundTasks,
    state: str = Query(default=""),
    code: str = Query(default=""),
    error: str = Query(default=""),
) -> RedirectResponse:
    """Google sends the browser here. Always redirect — never render JSON at a
    human.

    This route deliberately does **not** depend on the authenticated session.
    A browser arriving from Google carries no bearer token, so requiring one
    would make the callback 401 the moment `AUTH_DEV_BYPASS` is off. The
    `oauth_states` row is the credential: single use, short lived, and it names
    the user. The session is opened unscoped to read that row, then scoped to
    the user it names before anything is written.
    """
    web = settings.cors_origins[0]

    if error or not code or not state:
        return RedirectResponse(f"{web}/connecting?error=denied", status_code=303)

    # Which tenant this belongs to is precisely what we do not yet know, so the
    # state lookup cannot be tenant-scoped. Consuming it in the UPDATE makes
    # the check and the claim one atomic step — a replayed callback matches
    # nothing.
    async with get_sessionmaker()() as session:
        async with session.begin():
            row = (
                (
                    await session.execute(
                        text(
                            "UPDATE oauth_states SET consumed_at = now() "
                            "WHERE state = :state AND consumed_at IS NULL "
                            "  AND expires_at > now() "
                            "RETURNING user_id, return_path, code_verifier"
                        ),
                        {"state": state},
                    )
                )
                .mappings()
                .one_or_none()
            )

        if row is None:
            logger.warning("oauth_state_rejected")
            return RedirectResponse(f"{web}/connecting?error=expired", status_code=303)

        user_id = str(row["user_id"])
        return_path = str(row["return_path"])

        try:
            credentials = exchange_code(settings, code, row["code_verifier"])
            address = GmailClient(credentials).address()
        except Exception as exc:
            logger.warning("oauth_exchange_failed", error=str(exc)[:200])
            return RedirectResponse(f"{web}/connecting?error=exchange", status_code=303)

        if not credentials.refresh_token:
            # Without prompt=consent a returning user gets an access token
            # only, and the connection would silently die in an hour.
            logger.warning("oauth_no_refresh_token")
            return RedirectResponse(f"{web}/connecting?error=no_refresh_token", status_code=303)

        sealed = seal(
            credentials.refresh_token,
            master_key=settings.encryption_master_key or "",
            key_id=settings.encryption_key_id,
        )

        # Everything from here is this user's own data, so scope it.
        async with session.begin(), with_user(session, user_id):
            connection_id = (
                await session.execute(
                    text(
                        "INSERT INTO email_connections (user_id, provider, google_email, "
                        "  refresh_token_ciphertext, refresh_token_nonce, "
                        "  refresh_token_key_id, scopes, status) "
                        "VALUES (:uid, 'gmail', :email, :ct, :nonce, :kid, :scopes, "
                        "        'active') "
                        "ON CONFLICT (user_id, provider, google_email) DO UPDATE SET "
                        "  refresh_token_ciphertext = EXCLUDED.refresh_token_ciphertext, "
                        "  refresh_token_nonce = EXCLUDED.refresh_token_nonce, "
                        "  refresh_token_key_id = EXCLUDED.refresh_token_key_id, "
                        "  status = 'active', last_error = NULL, updated_at = now() "
                        "RETURNING id"
                    ),
                    {
                        "uid": uuid.UUID(user_id),
                        "email": address,
                        "ct": sealed.ciphertext,
                        "nonce": sealed.nonce,
                        "kid": sealed.key_id,
                        "scopes": SCOPES,
                    },
                )
            ).scalar_one()

            window = (
                await session.execute(
                    text("SELECT analysis_window_months FROM users WHERE id = :id"),
                    {"id": uuid.UUID(user_id)},
                )
            ).scalar_one_or_none() or 6

            run_id = (
                await session.execute(
                    text(
                        "INSERT INTO backfill_runs (user_id, connection_id, "
                        "  window_months, state) "
                        "VALUES (:uid, :cid, :w, 'pending') RETURNING id"
                    ),
                    {"uid": uuid.UUID(user_id), "cid": connection_id, "w": window},
                )
            ).scalar_one()

    # Fire and forget: the browser must not wait on a mailbox scan (§7.5).
    background.add_task(
        run_backfill,
        get_sessionmaker(),
        user_id=user_id,
        connection_id=uuid.UUID(str(connection_id)),
        run_id=uuid.UUID(str(run_id)),
        settings=settings,
        window_months=int(window),
    )
    logger.info("gmail_connected", backfill_run=str(run_id))
    return RedirectResponse(f"{web}{return_path}", status_code=303)


# --------------------------------------------------------------- sync, drop


@router.post("/v1/gmail/sync", summary="Catch-up sync")
async def gmail_sync(
    session: SessionDep,
    user_id: UserIdDep,
    settings: SettingsDep,
    background: BackgroundTasks,
) -> dict[str, str]:
    _require_google(settings)
    row = (
        (
            await session.execute(
                text(
                    "SELECT id FROM email_connections WHERE status = 'active' "
                    "ORDER BY created_at DESC LIMIT 1"
                )
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise ApiError(404, "not_found", "No Gmail account is connected.")

    window = (
        await session.execute(
            text("SELECT analysis_window_months FROM users WHERE id = :id"),
            {"id": uuid.UUID(user_id)},
        )
    ).scalar_one_or_none() or 6

    run_id = (
        await session.execute(
            text(
                "INSERT INTO backfill_runs (user_id, connection_id, window_months, state) "
                "VALUES (:uid, :cid, :w, 'pending') RETURNING id"
            ),
            {"uid": uuid.UUID(user_id), "cid": row["id"], "w": window},
        )
    ).scalar_one()

    background.add_task(
        run_backfill,
        get_sessionmaker(),
        user_id=user_id,
        connection_id=uuid.UUID(str(row["id"])),
        run_id=uuid.UUID(str(run_id)),
        settings=settings,
        window_months=int(window),
    )
    return {"status": "started"}


@router.delete("/v1/gmail", summary="Disconnect, revoke token, stop watch")
async def gmail_disconnect(
    session: SessionDep, user_id: UserIdDep, settings: SettingsDep
) -> dict[str, bool]:
    """Forget the connection and ask Google to revoke the grant.

    The stored token is deleted whether or not the revoke call succeeds: a
    disconnect the user asked for must not depend on Google being reachable.
    """
    rows = (
        (
            await session.execute(
                text(
                    "SELECT id, refresh_token_ciphertext, refresh_token_nonce, "
                    "refresh_token_key_id FROM email_connections"
                )
            )
        )
        .mappings()
        .all()
    )

    for row in rows:
        try:
            import httpx

            from todd.worker.lib.crypto import Sealed, try_unseal

            token = try_unseal(
                Sealed(
                    ciphertext=row["refresh_token_ciphertext"],
                    nonce=row["refresh_token_nonce"],
                    key_id=row["refresh_token_key_id"],
                ),
                master_key=settings.encryption_master_key or "",
            )
            if token:
                httpx.post(
                    "https://oauth2.googleapis.com/revoke",
                    data={"token": token},
                    timeout=5.0,
                )
        except Exception as exc:
            logger.warning("gmail_revoke_failed", error=str(exc)[:200])

    await session.execute(text("DELETE FROM email_connections"))
    return {"disconnected": True}
