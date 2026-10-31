"""Statement upload, listing and unlock.

Upload is synchronous for now: there is no queue yet, so the request parses
what it can and answers with counts the web app already understands. The
moment arq lands this becomes an enqueue and the same agent runs behind it.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, File, UploadFile
from sqlalchemy import text

from todd.api.deps import SessionDep, SettingsDep, UserIdDep
from todd.api.errors import ApiError
from todd.log import get_logger
from todd.schemas.api.base import ApiModel
from todd.schemas.api.models import UnlockRequest, UnlockResponse, UploadResponse
from todd.schemas.enums import DocumentStatus, Institution
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


@router.post("/documents", response_model=UploadResponse, summary="Upload statement PDFs")
async def upload_documents(
    session: SessionDep,
    user_id: UserIdDep,
    settings: SettingsDep,
    files: list[UploadFile] = File(...),  # noqa: B008
) -> UploadResponse:
    """Store each PDF, then parse it.

    Counts answer the three things the web app distinguishes: accepted,
    waiting on a password, and already seen. A duplicate is identified by
    content hash, so the same statement forwarded twice is one document.
    """
    if not files:
        raise ApiError(400, "bad_request", "No files were uploaded.")

    storage = build_storage(settings)
    accepted = locked = duplicates = 0

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
                    text("SELECT id, status::text AS status FROM documents WHERE sha256 = :sha"),
                    {"sha": digest},
                )
            )
            .mappings()
            .one_or_none()
        )

        if existing:
            duplicates += 1
            if existing["status"] == DocumentStatus.LOCKED.value:
                locked += 1
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

        result = await process_document(
            session,
            document_id=document_id,
            user_id=user_id,
            settings=settings,
            data=data,
        )
        if result.status is DocumentStatus.LOCKED:
            locked += 1
        else:
            accepted += 1

    logger.info("upload_handled", accepted=accepted, locked=locked, duplicates=duplicates)
    return UploadResponse(accepted=accepted, locked=locked, duplicates=duplicates)


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
