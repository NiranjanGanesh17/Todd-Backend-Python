"""Statement upload, listing and unlock.

Upload is synchronous for now: there is no queue yet, so the request parses
what it can and answers with counts the web app already understands. The
moment arq lands this becomes an enqueue and the same agent runs behind it.
"""

from __future__ import annotations

import asyncio
import uuid
from collections import defaultdict
from collections.abc import Coroutine
from typing import Any

from fastapi import APIRouter, File, Query, UploadFile
from sqlalchemy import text

from todd.api.deps import SessionDep, SettingsDep, UserIdDep
from todd.api.errors import ApiError
from todd.config import Settings
from todd.db.session import get_sessionmaker, with_user
from todd.log import get_logger
from todd.schemas.api.base import ApiModel
from todd.schemas.api.models import (
    DocumentProgress,
    QueuedDocument,
    UnlockRequest,
    UnlockResponse,
    UploadProgressResponse,
    UploadResponse,
)
from todd.schemas.enums import DocumentStatus, Institution
from todd.worker import progress
from todd.worker.agents.statement import process_document
from todd.worker.lib.storage import build_storage, safe_filename, sha256_of, storage_key

logger = get_logger(__name__)
router = APIRouter(prefix="/v1", tags=["documents"])

#: A bank statement is a few hundred kilobytes. Well above that it is not one,
#: and the request should be refused before anything is read into memory.
MAX_UPLOAD_BYTES = 20 * 1024 * 1024


class DocumentOut(ApiModel):
    id: str
    filename: str
    status: str
    institution: str | None
    error: str | None
    created_at: str


@router.get("/documents", response_model=list[DocumentOut], summary="Stored statements")
async def list_documents(session: SessionDep, user_id: UserIdDep) -> list[DocumentOut]:
    rows = (
        (
            await session.execute(
                text(
                    "SELECT id, filename, status::text AS status, "
                    "institution::text AS institution, error, created_at "
                    "FROM documents ORDER BY created_at DESC LIMIT 200"
                )
            )
        )
        .mappings()
        .all()
    )
    return [
        DocumentOut(
            id=str(r["id"]),
            filename=r["filename"],
            status=r["status"],
            institution=r["institution"],
            error=r["error"],
            created_at=r["created_at"].isoformat(),
        )
        for r in rows
    ]


#: Second lines, for the states the upload screen can show without a number
#: of its own. Written here so two clients cannot word them differently.
_PROGRESS_LABELS: dict[DocumentStatus, str] = {
    DocumentStatus.QUEUED: "Waiting",
    DocumentStatus.PARSING: "Opening",
    DocumentStatus.LOCKED: "Needs a password",
    DocumentStatus.FAILED: "We could not read this one",
}


def _progress_label(status: DocumentStatus, added: int, page: progress.Progress | None) -> str:
    if status is DocumentStatus.DONE or status is DocumentStatus.NEEDS_REVIEW:
        if added == 0:
            return "No transactions found"
        return f"{added} transaction{'' if added == 1 else 's'} added"
    if status is DocumentStatus.PARSING and page is not None and page.page_count:
        return f"Page {max(page.page, 1)} of {page.page_count}"
    return _PROGRESS_LABELS.get(status, "Waiting")


@router.get(
    "/documents/progress",
    response_model=UploadProgressResponse,
    summary="How far the upload has got",
)
async def upload_progress(
    session: SessionDep,
    user_id: UserIdDep,
    ids: str | None = Query(default=None, description="Comma-separated document ids."),
) -> UploadProgressResponse:
    """What the upload screen polls while it reads.

    Scoped to the ids the client was handed at upload, so the screen shows the
    files that person just chose and not every statement they have ever added.
    """
    wanted: list[uuid.UUID] = []
    for raw in (ids or "").split(","):
        token = raw.strip()
        if not token:
            continue
        try:
            wanted.append(uuid.UUID(token))
        except ValueError:
            raise ApiError(400, "bad_request", "Those document ids are not ids.") from None

    clause = "WHERE d.id = ANY(:ids)" if wanted else ""
    limit = "" if wanted else "ORDER BY d.created_at DESC LIMIT 10"
    rows = (
        (
            await session.execute(
                text(
                    "SELECT d.id, d.filename, d.status::text AS status, "
                    "       (SELECT count(*) FROM transactions t "
                    "        JOIN statements s ON s.id = t.statement_id "
                    "        WHERE s.document_id = d.id) AS added, "
                    "       d.created_at "
                    f"FROM documents d {clause} {limit}"
                ),
                {"ids": wanted} if wanted else {},
            )
        )
        .mappings()
        .all()
    )
    ordered = sorted(rows, key=lambda r: r["created_at"])

    documents: list[DocumentProgress] = []
    now_reading: str | None = None
    for row in ordered:
        status = DocumentStatus(row["status"])
        page = progress.read(str(row["id"]))
        added = int(row["added"] or 0)
        if status is DocumentStatus.PARSING and now_reading is None:
            now_reading = row["filename"]
        documents.append(
            DocumentProgress(
                id=str(row["id"]),
                filename=row["filename"],
                status=status,
                status_label=_progress_label(status, added, page),
                transactions_added=added,
                page=page.page if page else None,
                page_count=page.page_count if page else None,
            )
        )

    busy = any(
        document.status in (DocumentStatus.QUEUED, DocumentStatus.PARSING) for document in documents
    )
    return UploadProgressResponse(
        busy=busy,
        now_reading=now_reading,
        documents=documents,
        transactions_added=sum(document.transactions_added for document in documents),
    )


@router.post("/documents", response_model=UploadResponse, summary="Upload statement PDFs")
async def upload_documents(
    session: SessionDep,
    user_id: UserIdDep,
    settings: SettingsDep,
    files: list[UploadFile] = File(...),  # noqa: B008
) -> UploadResponse:
    """Store each PDF, then read it in the background.

    Storing is fast; reading a twelve-page statement is not. Parsing inside
    the request meant the browser held a POST open for a minute with nothing
    to show, and a phone that slept killed the upload. So this answers as soon
    as the files are on disk, and the screen follows them through
    `/v1/documents/progress`.

    A duplicate is identified by content hash, so the same statement forwarded
    twice is one document.
    """
    if not files:
        raise ApiError(400, "bad_request", "No files were uploaded.")

    storage = build_storage(settings)
    duplicates = 0
    queued: list[QueuedDocument] = []
    #: Everything to read: the new files, and the ones being offered again.
    to_read: list[str] = []

    for upload in files:
        data = await upload.read()
        if not data:
            continue
        if len(data) > MAX_UPLOAD_BYTES:
            raise ApiError(413, "payload_too_large", f"{upload.filename} is too large.")

        digest = sha256_of(data)
        existing = (
            (
                await session.execute(
                    text(
                        "SELECT id, status::text AS status, storage_key "
                        "FROM documents WHERE sha256 = :sha"
                    ),
                    {"sha": digest},
                )
            )
            .mappings()
            .one_or_none()
        )

        if existing:
            # A file we already hold is still one of the files this person
            # just chose, so it goes on the screen with the others — and it is
            # read again.
            #
            # Deduplicating the *document* is right: the same statement
            # forwarded twice must not become two. Deduplicating the *reading*
            # was not. Uploading a statement again is how someone says "take
            # another look at this", and the parser does move on — the run
            # that fixed narration wrapping left every row parsed before it
            # cut short, with no way to repair them. Re-reading is safe:
            # `upsert_statement` keys on the account and period, and
            # `converge_transactions` deletes what the statement no longer
            # claims before inserting what it does, so rows are corrected
            # rather than duplicated, and a category the user set survives.
            # The row can outlive the object: an erasure that ran against a
            # shared storage directory, a restore that brought the database
            # back but not the bucket. Re-uploading the file is the user
            # saying "here it is again", and without this the re-read fails
            # on a key that points at nothing.
            if not storage.exists(existing["storage_key"]):
                storage.put(existing["storage_key"], data)
                logger.info("document_file_restored", document_id=str(existing["id"]))

            duplicates += 1
            queued.append(
                QueuedDocument(id=str(existing["id"]), filename=upload.filename or "statement")
            )
            to_read.append(str(existing["id"]))
            continue

        document_id = uuid.uuid4()
        filename = safe_filename(upload.filename or "statement.pdf")
        key = storage_key(user_id, str(document_id), filename)
        storage.put(key, data)

        await session.execute(
            text(
                "INSERT INTO documents (id, user_id, filename, storage_key, sha256, "
                "  size_bytes, status) "
                "VALUES (:id, :uid, :fn, :key, :sha, :size, 'queued')"
            ),
            {
                "id": document_id,
                "uid": uuid.UUID(user_id),
                "fn": filename,
                "key": key,
                "sha": digest,
                "size": len(data),
            },
        )

        queued.append(QueuedDocument(id=str(document_id), filename=filename))
        to_read.append(str(document_id))

    if to_read:
        # A task, not FastAPI's `BackgroundTasks`: those run after the
        # response has been written, and this stack's middleware is the kind
        # that drops them. A task also starts reading while the response is
        # still on the wire, which is the second the screen opens.
        _spawn(_parse_queued(to_read, user_id=user_id, settings=settings), user_id=user_id)

    accepted = len(to_read) - duplicates
    logger.info("upload_handled", accepted=accepted, duplicates=duplicates)
    return UploadResponse(accepted=accepted, duplicates=duplicates, documents=queued)


#: Live parse tasks, against the user they are reading for. Without a
#: reference the event loop is free to collect a task mid-flight, and the
#: upload would stop halfway with nothing logged. The owner is kept so that
#: erasing an account can stop the reads still writing into it.
_RUNNING: dict[asyncio.Task[None], str] = {}


def _spawn(work: Coroutine[Any, Any, None], *, user_id: str) -> None:
    task = asyncio.create_task(work)
    _RUNNING[task] = user_id
    task.add_done_callback(lambda done: _RUNNING.pop(done, None))


def cancel_parses(user_id: str) -> int:
    """Stop whatever this user has in flight. Returns how many were stopped.

    An erasure calls this first. A parse that outlives one finishes against a
    tenant that is no longer there: in production the insert fails on the
    foreign key and the statement is simply lost, but where the user row is
    recreated for the next request — the dev bypass does exactly that — the
    rows it writes land in the account the user had just emptied, and the
    clean slate they asked for arrives with last month's transactions on it.
    """
    stopped = [task for task, owner in _RUNNING.items() if owner == user_id and not task.done()]
    for task in stopped:
        task.cancel()
    return len(stopped)


async def resume_unfinished(settings: Settings) -> int:
    """Pick up documents a previous process was reading when it stopped.

    Parsing lives in this process (§13 — there is no queue yet), so a restart
    mid-upload used to leave a document at `parsing` with nothing behind it,
    and the upload screen would say "Opening…" for ever. Nothing can be
    parsing in a process that has only just started, so on the way up those go
    back to `queued` and are read again. Re-reading is safe: the statement
    upsert and `converge_transactions` both converge rather than append.

    Runs unscoped, because it is looking across every tenant before any
    request has said who is asking; the work itself is scoped per user.
    """
    async with get_sessionmaker()() as session, session.begin():
        rows = (
            (
                await session.execute(
                    text(
                        "UPDATE documents SET status = 'queued', updated_at = now() "
                        "WHERE status IN ('queued', 'parsing') "
                        "RETURNING id, user_id"
                    )
                )
            )
            .mappings()
            .all()
        )

    by_user: dict[str, list[str]] = defaultdict(list)
    for row in rows:
        by_user[str(row["user_id"])].append(str(row["id"]))

    for owner, ids in by_user.items():
        logger.info("resuming_unfinished_documents", user_id=owner, documents=len(ids))
        _spawn(_parse_queued(ids, user_id=owner, settings=settings), user_id=owner)
    return len(rows)


async def _parse_queued(document_ids: list[str], *, user_id: str, settings: Settings) -> None:
    """Read the uploaded files, one at a time, after the response has gone.

    Its own session: the request's is closed by the time this runs. One
    document per transaction, so a statement that throws does not take the
    ones already written down with it.
    """
    storage = build_storage(settings)
    for document_id in document_ids:
        try:
            async with (
                get_sessionmaker()() as session,
                session.begin(),
                with_user(session, user_id) as scoped,
            ):
                row = (
                    (
                        await scoped.execute(
                            text(
                                "SELECT storage_key, institution::text AS institution "
                                "FROM documents WHERE id = :id"
                            ),
                            {"id": uuid.UUID(document_id)},
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if row is None:
                    continue
                await process_document(
                    scoped,
                    document_id=uuid.UUID(document_id),
                    user_id=user_id,
                    settings=settings,
                    data=storage.get(row["storage_key"]),
                    institution=_institution(row["institution"]),
                )
        except Exception:
            # One unreadable file must not stop the queue. The document keeps
            # whatever status it reached, and the screen shows that.
            logger.exception("upload_parse_failed", document_id=document_id)


def _institution(value: str | None) -> Institution:
    try:
        return Institution(value or "unknown")
    except ValueError:
        return Institution.UNKNOWN


async def _unlock(
    session: SessionDep,
    user_id: str,
    settings: SettingsDep,
    *,
    body: UnlockRequest,
    document_id: uuid.UUID | None,
) -> UnlockResponse:
    """Retry locked documents with the password the user just typed.

    Scoped to one document when given one, otherwise every locked document the
    user has — which is what the web app's bank-level Unlock means.
    """
    clause = "AND id = :did" if document_id else ""
    params: dict[str, object] = {}
    if document_id:
        params["did"] = document_id

    rows = (
        (
            await session.execute(
                text(
                    "SELECT id, storage_key, institution::text AS institution "
                    f"FROM documents WHERE status = 'locked' {clause}"
                ),
                params,
            )
        )
        .mappings()
        .all()
    )
    if not rows:
        return UnlockResponse(unlocked=0, still_locked=0)

    storage = build_storage(settings)
    unlocked = still_locked = 0

    for row in rows:
        try:
            data = storage.get(row["storage_key"])
        except FileNotFoundError:
            still_locked += 1
            continue
        result = await process_document(
            session,
            document_id=uuid.UUID(str(row["id"])),
            user_id=user_id,
            settings=settings,
            data=data,
            typed_password=body.password,
            remember_password=body.remember,
            # What the document already knows. Dropping it here is how every
            # unlocked statement used to land in one "unknown bank" account:
            # the sender's bank was recorded at ingest and then thrown away at
            # the one moment the file could finally be read.
            institution=_institution(row["institution"]),
        )
        if result.status is DocumentStatus.LOCKED:
            still_locked += 1
        else:
            unlocked += 1

    logger.info("unlock_handled", unlocked=unlocked, still_locked=still_locked)
    return UnlockResponse(unlocked=unlocked, still_locked=still_locked)


@router.post(
    "/documents/{documentId}/unlock",
    response_model=UnlockResponse,
    summary="Password for one locked PDF",
)
async def unlock_document(
    documentId: str,  # noqa: N803 — the path parameter the Node spec publishes
    body: UnlockRequest,
    session: SessionDep,
    user_id: UserIdDep,
    settings: SettingsDep,
) -> UnlockResponse:
    try:
        parsed = uuid.UUID(documentId)
    except ValueError as exc:
        raise ApiError(400, "bad_request", "That is not a document id.") from exc
    return await _unlock(session, user_id, settings, body=body, document_id=parsed)


@router.post("/unlock", response_model=UnlockResponse, summary="Password for a bank's statements")
async def unlock_account(
    body: UnlockRequest,
    session: SessionDep,
    user_id: UserIdDep,
    settings: SettingsDep,
) -> UnlockResponse:
    return await _unlock(session, user_id, settings, body=body, document_id=None)
