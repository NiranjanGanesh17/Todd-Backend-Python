"""Read stored statements again, with whatever the parser knows today.

Parsing improves; statements already in the ledger do not. A row written by an
older parser keeps that parser's mistakes for ever — a narration cut short, a
counterparty read as `17-2@ok`, a reference missing — and every later rung
works from the damaged text.

This re-reads the PDFs we still hold. Safe to run as often as you like:
`upsert_statement` keys on the account and period, and `converge_transactions`
deletes what a statement no longer claims before inserting what it does, so a
re-read corrects rows rather than duplicating them. A category the user set
themselves survives, because the ladder never overwrites `user_rule`.

A password-protected statement can only be re-read if its password is stored —
tick "Remember for next month" when unlocking. Without it the file stays as it
is, and nothing is lost.

    uv run python scripts/reparse.py --dry-run
    uv run python scripts/reparse.py
"""

from __future__ import annotations

import asyncio
import pathlib
import sys
import uuid

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from sqlalchemy import text
from todd.config import get_settings
from todd.db.session import dispose_engine, get_sessionmaker, init_engine, with_user
from todd.schemas.enums import DocumentStatus
from todd.worker.agents.statement import process_document
from todd.worker.lib.storage import build_storage


async def main() -> None:
    settings = get_settings()
    dry_run = "--dry-run" in sys.argv
    init_engine(settings)
    storage = build_storage(settings)
    user_id = settings.auth_dev_user_id

    async with get_sessionmaker()() as session:
        async with session.begin(), with_user(session, user_id) as scoped:
            documents = (
                (
                    await scoped.execute(
                        text(
                            "SELECT d.id, d.filename, d.storage_key, d.status::text AS status, "
                            "       (SELECT count(*) FROM transactions t "
                            "        JOIN statements s ON s.id = t.statement_id "
                            "        WHERE s.document_id = d.id) AS rows "
                            "FROM documents d WHERE d.status IN ('done', 'needs_review') "
                            "ORDER BY d.created_at"
                        )
                    )
                )
                .mappings()
                .all()
            )

        print(f"{len(documents)} statement(s) to re-read" + (" (dry run)" if dry_run else ""))
        if dry_run:
            for row in documents:
                print(f"  {row['filename'][:52]:52s} {row['status']:12s} {row['rows']:>4} rows")
            await dispose_engine()
            return

        for row in documents:
            before = int(row["rows"])
            async with session.begin(), with_user(session, user_id) as scoped:
                try:
                    data = storage.get(row["storage_key"])
                except FileNotFoundError:
                    print(f"  {row['filename'][:52]:52s} file is gone, left alone")
                    continue

                result = await process_document(
                    scoped,
                    document_id=uuid.UUID(str(row["id"])),
                    user_id=user_id,
                    settings=settings,
                    data=data,
                )

                # A statement we can no longer open is not a statement we have
                # lost: its rows are still in the ledger, so the document row
                # is put back the way the successful parse left it.
                if result.status is DocumentStatus.LOCKED:
                    await scoped.execute(
                        text(
                            "UPDATE documents d SET status = CASE WHEN s.reconciled "
                            "    THEN 'done' ELSE 'needs_review' END::document_status, "
                            "  updated_at = now() "
                            "FROM statements s WHERE s.document_id = d.id AND d.id = :id"
                        ),
                        {"id": uuid.UUID(str(row["id"]))},
                    )
                    print(f"  {row['filename'][:52]:52s} needs its password, left as it was")
                    continue

                print(
                    f"  {row['filename'][:52]:52s} {result.status.value:12s} "
                    f"{before:>4} -> {result.rows_written:<4} rows"
                )

    await dispose_engine()


if __name__ == "__main__":
    asyncio.run(main())
