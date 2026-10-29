"""Statement upload, listing and unlock.

Listing is built and reads the database. Upload and unlock are the statement
agent's front door (steps 6 and 7) and return 501 until it exists — accepting a
PDF we cannot parse would leave the user watching a queue that never drains.
"""

from __future__ import annotations

from fastapi import APIRouter, File, UploadFile
from sqlalchemy import text

from todd.api.deps import SessionDep, UserIdDep
from todd.api.errors import ApiError
from todd.schemas.api.base import ApiModel
from todd.schemas.api.models import UnlockRequest, UnlockResponse, UploadResponse

router = APIRouter(prefix="/v1", tags=["documents"])

_NOT_YET = "Statement parsing is not built yet."


class DocumentOut(ApiModel):
    id: str
    filename: str
    status: str
    institution: str | None
    created_at: str


@router.get("/documents", response_model=list[DocumentOut], summary="Stored statements")
async def list_documents(session: SessionDep, user_id: UserIdDep) -> list[DocumentOut]:
    rows = (
        (
            await session.execute(
                text(
                    "SELECT id, filename, status::text AS status, institution::text AS "
                    "institution, created_at FROM documents ORDER BY created_at DESC LIMIT 200"
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
            created_at=r["created_at"].isoformat(),
        )
        for r in rows
    ]


@router.post("/documents", response_model=UploadResponse, summary="Upload statement PDFs")
async def upload_documents(files: list[UploadFile] = File(...)) -> UploadResponse:  # noqa: B008
    raise ApiError(501, "not_implemented", _NOT_YET)


@router.post(
    "/documents/{documentId}/unlock",
    response_model=UnlockResponse,
    summary="Password for one locked PDF",
)
async def unlock_document(documentId: str, body: UnlockRequest) -> UnlockResponse:  # noqa: N803
    raise ApiError(501, "not_implemented", _NOT_YET)


@router.post("/unlock", response_model=UnlockResponse, summary="Password for a bank's statements")
async def unlock_account(body: UnlockRequest) -> UnlockResponse:
    raise ApiError(501, "not_implemented", _NOT_YET)
