"""DELETE /v1/account — erase everything one person owns.

Two jobs, and they happen to be the same call. It is the DPDP erasure right
the Privacy section of the profile screen promises ("Deleting your account
removes your statements and transactions"), and it is the only honest way to
start over: a ledger carries rows written by every parser that has ever run
over it, and a statement read by an older one keeps that reading for good.
Re-uploading corrects the statements we still hold, but nothing repairs an
account the parser split in two, or a row whose statement was lost.

What goes: every row keyed on the user, by the `ON DELETE cascade` on
`users`, and every stored PDF, by prefix — storage keys are
`users/{user_id}/…` precisely so that this is one call and not a scan.

What stays: the shared reference data (categories, the merchant dictionary),
which is not personal and is not theirs.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter
from sqlalchemy import text

from todd.api.deps import SessionDep, SettingsDep, UserIdDep
from todd.api.routes.documents import cancel_parses
from todd.log import get_logger
from todd.schemas.api.models import DeleteAccountResponse, DeletedCounts
from todd.worker import progress
from todd.worker.lib.storage import build_storage

logger = get_logger(__name__)
router = APIRouter(prefix="/v1", tags=["account"])

#: Counted before the delete, because afterwards there is nothing to count.
COUNTS = """
SELECT (SELECT count(*) FROM transactions) AS transactions,
       (SELECT count(*) FROM statements)   AS statements,
       (SELECT count(*) FROM accounts)     AS accounts,
       (SELECT count(*) FROM documents)    AS documents
"""


@router.delete(
    "/account",
    response_model=DeleteAccountResponse,
    summary="Erase every statement, transaction and file",
)
async def delete_account(
    session: SessionDep, user_id: UserIdDep, settings: SettingsDep
) -> DeleteAccountResponse:
    """Irreversible, and deliberately not a soft delete.

    There is no undo and nothing is kept back: a "delete" that hides rows is
    not the thing the screen says it is.

    Under the dev bypass the account comes back — empty — on the very next
    request, because the bypass user is created on demand. That is what makes
    this a reset in development and an erasure in production, from one call.
    """
    stopped = cancel_parses(user_id)

    counts = (await session.execute(text(COUNTS))).mappings().one()
    documents = (await session.execute(text("SELECT id FROM documents"))).scalars().all()

    # The parse board is in memory, so it survives the rows it describes.
    # Left alone, a document id the upload screen is still polling would keep
    # reporting "Page 3 of 6" for a statement that no longer exists.
    for document_id in documents:
        progress.clear(str(document_id))

    await session.execute(text("DELETE FROM users WHERE id = :id"), {"id": uuid.UUID(user_id)})

    files = 0
    try:
        files = build_storage(settings).delete_prefix(f"users/{user_id}")
    except Exception as exc:
        # The rows are the part that matters and they are already gone. A
        # statement whose document row no longer exists is unreachable: it is
        # not read, not listed and not counted. Failing the erasure here would
        # leave the user with their data and an error message.
        logger.warning("account_files_not_removed", error=str(exc)[:200])

    removed = DeletedCounts(
        transactions=int(counts["transactions"]),
        statements=int(counts["statements"]),
        accounts=int(counts["accounts"]),
        documents=int(counts["documents"]),
        files=files,
    )
    logger.info("account_erased", **removed.model_dump(), parses_stopped=stopped)
    return DeleteAccountResponse(removed=removed)
