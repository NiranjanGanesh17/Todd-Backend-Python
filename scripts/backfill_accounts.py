"""Give existing statements the account they actually belong to.

Until now `process_document` resolved every statement to one account —
institution unknown, type savings, no number — because the unlock path dropped
the institution the document already carried and nothing ever read the bank's
name off the page. Profile showed one row, "Unknown bank savings", for four
real accounts.

The parser is fixed; this moves the rows that were written before it was.
For each document with a statement, in order of preference:

1. re-open the PDF from storage and read its header (the same detector the
   parser now uses), which is the only source that gives a masked number;
2. fall back to `documents.institution`, recorded from the Gmail sender at
   ingest, which names the bank but not the account.

Nothing is invented: a document that gives neither keeps the account it has.
Statements, transactions and documents are re-pointed together, and accounts
left with nothing attached are removed.

    uv run python scripts/backfill_accounts.py --dry-run
    uv run python scripts/backfill_accounts.py
"""

from __future__ import annotations

import asyncio
import contextlib
import pathlib
import sys
import uuid
from collections import defaultdict

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from todd.config import get_settings
from todd.db.session import dispose_engine, get_sessionmaker, init_engine, with_user
from todd.parsers.institution import AccountHint, card_from_filename, detect_account
from todd.schemas.enums import AccountType, Institution
from todd.worker.agents.statement import open_with_ladder, password_candidates, resolve_account
from todd.worker.lib.storage import build_storage


async def _hint_for(
    *,
    filename: str,
    storage_key: str,
    institution: str | None,
    storage: object,
    passwords: list[str],
) -> tuple[AccountHint, str]:
    """What we can establish about one document's account, and from where."""
    fallback = Institution.UNKNOWN
    with contextlib.suppress(ValueError):
        fallback = Institution(institution or "unknown")

    try:
        data = storage.get(storage_key)  # type: ignore[attr-defined]
        document, _password, _tries = open_with_ladder(data, passwords)
    except Exception:  # a missing or unopenable file is simply no evidence
        document = None

    if document is not None and document.pages:
        hint = detect_account([line.text for line in document.pages[0].lines], fallback=fallback)
        if hint.identified or hint.masked_number:
            return hint, "statement header"

    # Still locked, so the page cannot be read. A masked card number in the
    # filename is the bank's own writing and is enough to keep a card apart
    # from the savings account at the same bank.
    card = card_from_filename(filename)
    if card:
        return AccountHint(fallback, AccountType.CREDIT_CARD, card), "filename"

    return AccountHint(fallback, AccountType.SAVINGS, None), "sender"


async def run(session: AsyncSession, *, user_id: str, settings: object, dry_run: bool) -> None:
    storage = build_storage(settings)  # type: ignore[arg-type]
    passwords = await password_candidates(
        session,
        user_id=user_id,
        settings=settings,  # type: ignore[arg-type]
        typed=None,
    )
    print(f"remembered passwords: {len(passwords)}")

    documents = (
        (
            await session.execute(
                text(
                    "SELECT d.id, d.filename, d.storage_key, d.institution::text AS institution, "
                    "       s.id AS statement_id, s.account_id "
                    "FROM documents d JOIN statements s ON s.document_id = d.id "
                    "ORDER BY d.created_at"
                )
            )
        )
        .mappings()
        .all()
    )

    moves: dict[uuid.UUID, list[str]] = defaultdict(list)
    for row in documents:
        hint, source = await _hint_for(
            filename=row["filename"],
            storage_key=row["storage_key"],
            institution=row["institution"],
            storage=storage,
            passwords=passwords,
        )
        target = await resolve_account(
            session,
            user_id=user_id,
            institution=hint.institution,
            account_type=hint.account_type,
            masked_number=hint.masked_number,
        )
        masked = f"··{hint.masked_number}" if hint.masked_number else "no number"
        print(
            f"  {row['filename'][:46]:46s} -> {hint.institution.value} "
            f"{hint.account_type.value} {masked}  ({source})"
        )
        if target != row["account_id"]:
            moves[target].append(str(row["statement_id"]))

    if not moves:
        print("nothing to move")
        return

    total = sum(len(ids) for ids in moves.values())
    print(f"\n{total} statement(s) move to {len(moves)} account(s)")
    if dry_run:
        print("dry run — nothing written")
        return

    for account_id, statement_ids in moves.items():
        params = {"aid": account_id, "sids": statement_ids}
        await session.execute(
            text(
                "UPDATE transactions SET account_id = :aid, updated_at = now() "
                "WHERE statement_id = ANY(CAST(:sids AS uuid[]))"
            ),
            params,
        )
        await session.execute(
            text(
                "UPDATE documents SET account_id = :aid, updated_at = now() "
                "WHERE id IN (SELECT document_id FROM statements "
                "             WHERE id = ANY(CAST(:sids AS uuid[])))"
            ),
            params,
        )
        await session.execute(
            text(
                "UPDATE statements SET account_id = :aid, updated_at = now() "
                "WHERE id = ANY(CAST(:sids AS uuid[]))"
            ),
            params,
        )

    orphans = await session.execute(
        text(
            "DELETE FROM accounts a WHERE NOT EXISTS "
            "  (SELECT 1 FROM statements s WHERE s.account_id = a.id) "
            "AND NOT EXISTS (SELECT 1 FROM documents d WHERE d.account_id = a.id) "
            "AND NOT EXISTS (SELECT 1 FROM transactions t WHERE t.account_id = a.id) "
            "RETURNING id"
        )
    )
    print(f"removed {len(orphans.all())} account(s) left with nothing attached")


async def main() -> None:
    settings = get_settings()
    dry_run = "--dry-run" in sys.argv
    init_engine(settings)
    async with (
        get_sessionmaker()() as session,
        session.begin(),
        with_user(session, settings.auth_dev_user_id) as scoped,
    ):
        await run(scoped, user_id=settings.auth_dev_user_id, settings=settings, dry_run=dry_run)
    await dispose_engine()


if __name__ == "__main__":
    asyncio.run(main())
