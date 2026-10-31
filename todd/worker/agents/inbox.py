"""Gmail backfill: mail in, documents out.

Fire-and-forget. The OAuth redirect returns before any mail is read, so the
browser lands on a progress screen and `/v1/gmail/status` is the only way the
user learns what was found (§7.5, §8.14).

Per-message try/except throughout: one malformed message must not abandon the
rest of the mailbox.
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from todd.config import Settings
from todd.log import get_logger
from todd.parsers.senders import build_gmail_query, classify_message
from todd.schemas.enums import RawMessageKind
from todd.worker.agents.statement import process_document
from todd.worker.lib.crypto import Sealed, try_unseal
from todd.worker.lib.gmail import GmailClient, credentials_from_refresh_token
from todd.worker.lib.storage import build_storage, safe_filename, sha256_of, storage_key

logger = get_logger(__name__)

#: One mailbox scan. Above this the window is too wide to be a first connect.
MAX_MESSAGES = 500


async def run_backfill(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    user_id: str,
    connection_id: uuid.UUID,
    run_id: uuid.UUID,
    settings: Settings,
    window_months: int,
) -> None:
    """Scan the mailbox and queue every statement PDF found.

    Owns its own sessions: it outlives the request that started it, so it
    cannot borrow that request's transaction.
    """
    log = logger.bind(run_id=str(run_id), user_id=user_id)

    try:
        async with sessionmaker() as session, session.begin():
            await _set_scope(session, user_id)
            refresh_token = await _refresh_token_for(session, connection_id, settings)
            await session.execute(
                text("UPDATE backfill_runs SET state='running', started_at=now() WHERE id=:id"),
                {"id": run_id},
            )

        if not refresh_token:
            await _finish(sessionmaker, user_id, run_id, "failed", "no usable refresh token")
            return

        client = GmailClient(credentials_from_refresh_token(settings, refresh_token))
        query = build_gmail_query(window_months)
        log.info("backfill_search", query_domains=query.count("from:"))

        ids = client.search_ids(query, limit=MAX_MESSAGES)
        async with sessionmaker() as session, session.begin():
            await _set_scope(session, user_id)
            await session.execute(
                text("UPDATE backfill_runs SET messages_found=:n WHERE id=:id"),
                {"id": run_id, "n": len(ids)},
            )
        log.info("backfill_found", messages=len(ids))

        processed = statements = alerts = 0
        for message_id in ids:
            try:
                found_statement, found_alert = await _handle_message(
                    sessionmaker,
                    client=client,
                    message_id=message_id,
                    user_id=user_id,
                    connection_id=connection_id,
                    settings=settings,
                )
                statements += int(found_statement)
                alerts += int(found_alert)
            except Exception as exc:
                log.warning("backfill_message_failed", error=str(exc)[:200])
            processed += 1

            if processed % 10 == 0 or processed == len(ids):
                async with sessionmaker() as session, session.begin():
                    await _set_scope(session, user_id)
                    await session.execute(
                        text(
                            "UPDATE backfill_runs SET messages_processed=:p, "
                            "statements_found=:s, alerts_found=:a WHERE id=:id"
                        ),
                        {"id": run_id, "p": processed, "s": statements, "a": alerts},
                    )

        await _finish(sessionmaker, user_id, run_id, "done", None)
        log.info("backfill_done", processed=processed, statements=statements, alerts=alerts)

    except Exception as exc:
        logger.exception("backfill_failed", run_id=str(run_id))
        await _finish(sessionmaker, user_id, run_id, "failed", str(exc)[:500])


async def _handle_message(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    client: GmailClient,
    message_id: str,
    user_id: str,
    connection_id: uuid.UUID,
    settings: Settings,
) -> tuple[bool, bool]:
    """Store one message, and parse any statement PDF it carries."""
    message = client.fetch(message_id)
    classified = classify_message(
        from_address=message.from_address,
        subject=message.subject,
        has_pdf_attachment=message.has_pdf,
    )
    if classified is None or classified.kind is RawMessageKind.IGNORE:
        return False, False

    received = dt.datetime.fromtimestamp(message.received_at_ms / 1000, tz=dt.UTC)
    digest = sha256_of(f"{message_id}|{message.subject}".encode())

    async with sessionmaker() as session, session.begin():
        await _set_scope(session, user_id)
        # Idempotent: the same message arriving twice has no effect.
        existing = (
            await session.execute(
                text("SELECT 1 FROM raw_messages WHERE provider_msg_id = :pid"),
                {"pid": message_id},
            )
        ).scalar_one_or_none()
        if existing:
            return False, False

        await session.execute(
            text(
                "INSERT INTO raw_messages (user_id, connection_id, provider_msg_id, "
                "  from_addr, subject, received_at, kind, institution, sha256) "
                "VALUES (:uid, :cid, :pid, :frm, :subj, :recv, :kind, :inst, :sha) "
                "ON CONFLICT DO NOTHING"
            ),
            {
                "uid": uuid.UUID(user_id),
                "cid": connection_id,
                "pid": message_id,
                "frm": message.from_address[:320],
                "subj": (message.subject or "")[:2000],
                "recv": received,
                "kind": classified.kind.value,
                "inst": classified.institution.value,
                "sha": digest,
            },
        )

    if classified.kind is RawMessageKind.TXN_ALERT:
        return False, True

    # Only STATEMENT remains: IGNORE returned above, and the enum has no fourth
    # member. Alerts are stored for the alert agent (§9) and parsed by nobody yet.

    stored_any = False
    storage = build_storage(settings)
    for attachment in message.attachments:
        if not attachment.filename.lower().endswith(".pdf"):
            continue
        pdf_digest = sha256_of(attachment.data)

        async with sessionmaker() as session, session.begin():
            await _set_scope(session, user_id)
            seen = (
                await session.execute(
                    text("SELECT 1 FROM documents WHERE sha256 = :sha"), {"sha": pdf_digest}
                )
            ).scalar_one_or_none()
            if seen:
                continue

            document_id = uuid.uuid4()
            filename = safe_filename(attachment.filename)
            key = storage_key(user_id, str(document_id), filename)
            storage.put(key, attachment.data)

            await session.execute(
                text(
                    "INSERT INTO documents (id, user_id, filename, storage_key, sha256, "
                    "  size_bytes, institution, status) "
                    "VALUES (:id, :uid, :fn, :key, :sha, :size, :inst, 'queued')"
                ),
                {
                    "id": document_id,
                    "uid": uuid.UUID(user_id),
                    "fn": filename,
                    "key": key,
                    "sha": pdf_digest,
                    "size": len(attachment.data),
                    "inst": classified.institution.value,
                },
            )
            await process_document(
                session,
                document_id=document_id,
                user_id=user_id,
                settings=settings,
                data=attachment.data,
                institution=classified.institution,
            )
            stored_any = True

    return stored_any, False


async def _set_scope(session: AsyncSession, user_id: str) -> None:
    await session.execute(text("SET LOCAL ROLE app_user"))
    await session.execute(text("SELECT set_config('app.user_id', :uid, true)"), {"uid": user_id})


async def _refresh_token_for(
    session: AsyncSession, connection_id: uuid.UUID, settings: Settings
) -> str | None:
    row = (
        (
            await session.execute(
                text(
                    "SELECT refresh_token_ciphertext, refresh_token_nonce, "
                    "refresh_token_key_id FROM email_connections WHERE id = :id"
                ),
                {"id": connection_id},
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        return None
    return try_unseal(
        Sealed(
            ciphertext=row["refresh_token_ciphertext"],
            nonce=row["refresh_token_nonce"],
            key_id=row["refresh_token_key_id"],
        ),
        master_key=settings.encryption_master_key or "",
    )


async def _finish(
    sessionmaker: async_sessionmaker[AsyncSession],
    user_id: str,
    run_id: uuid.UUID,
    state: str,
    error: str | None,
) -> None:
    async with sessionmaker() as session, session.begin():
        await _set_scope(session, user_id)
        await session.execute(
            text("UPDATE backfill_runs SET state=:st, finished_at=now(), error=:err WHERE id=:id"),
            {"id": run_id, "st": state, "err": error},
        )
