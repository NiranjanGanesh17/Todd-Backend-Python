"""Undo one statement having been read into two accounts.

Re-reading a statement used to resolve it to a *different* account whenever
the new read managed to see a masked number the first read had missed. The
natural key for an account includes that number, so a second account was
created; a transaction's fingerprint is scoped to its account, so every row
was written again under it; and the first statement stayed reconciled, so the
report counted both. One statement, read twice, became two of everything.

The parser no longer does this — a document keeps the account it already has,
and a statement is keyed on its document. This repairs ledgers written before
that, in two steps:

1. **Superseded statements.** Where one document has more than one statement,
   the older ones and their transactions go. The newest read is the one the
   parser would keep today.
2. **Split accounts.** Accounts of the same bank and type are merged onto the
   oldest, carrying their statements, documents and transactions with them.
   The masked number survives only if every account that claimed one agreed:
   where the reads disagree, they cannot all be right, and no number is
   better than a wrong one on screen.

Nothing is invented and nothing is deduplicated by guesswork: only rows whose
statement has been superseded are removed.

    uv run python scripts/repair_split_accounts.py --dry-run
    uv run python scripts/repair_split_accounts.py
"""

from __future__ import annotations

import asyncio
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from todd.config import get_settings
from todd.db.session import dispose_engine, get_sessionmaker, init_engine, with_user

#: A document's statements, oldest first, where it has more than one.
SUPERSEDED = """
SELECT st.id, d.filename, st.created_at
FROM statements st
JOIN documents d ON d.id = st.document_id
WHERE EXISTS (
  SELECT 1 FROM statements newer
   WHERE newer.document_id = st.document_id
     AND newer.created_at > st.created_at)
ORDER BY d.filename, st.created_at
"""

#: Accounts of one bank and type that should never have been more than one.
SPLITS = """
SELECT institution::text AS institution, type::text AS type,
       array_agg(id ORDER BY created_at) AS ids,
       array_agg(masked_number ORDER BY created_at) AS numbers
FROM accounts
GROUP BY institution, type
HAVING count(*) > 1
"""


async def _drop_superseded(session: AsyncSession, *, dry_run: bool) -> int:
    rows = (await session.execute(text(SUPERSEDED))).mappings().all()
    if not rows:
        print("no superseded statements")
        return 0

    ids = [row["id"] for row in rows]
    counted = (
        await session.execute(
            text("SELECT count(*) FROM transactions WHERE statement_id = ANY(:ids)"),
            {"ids": ids},
        )
    ).scalar_one()
    for row in rows:
        when = f"{row['created_at']:%Y-%m-%d %H:%M}"
        print(f"  superseded: {row['filename'][:44]:44s} read at {when}")
    print(f"  {len(ids)} statement(s) and {counted} transaction(s) to remove")

    if not dry_run:
        await session.execute(
            text("DELETE FROM transactions WHERE statement_id = ANY(:ids)"), {"ids": ids}
        )
        await session.execute(text("DELETE FROM statements WHERE id = ANY(:ids)"), {"ids": ids})
    return int(counted)


async def _merge_splits(session: AsyncSession, *, dry_run: bool) -> int:
    groups = (await session.execute(text(SPLITS))).mappings().all()
    merged = 0
    for group in groups:
        keep, *rest = group["ids"]
        claimed = {n for n in group["numbers"] if n}
        agreed = claimed.pop() if len(claimed) == 1 else None
        print(
            f"  {group['institution']} {group['type']}: {len(rest)} extra account(s), "
            f"numbers claimed {sorted(n for n in group['numbers'] if n) or 'none'} "
            f"-> {agreed or 'no number'}"
        )
        merged += len(rest)
        if dry_run:
            continue

        for table in ("transactions", "statements", "documents"):
            await session.execute(
                text(f"UPDATE {table} SET account_id = :keep WHERE account_id = ANY(:rest)"),
                {"keep": keep, "rest": rest},
            )
        await session.execute(
            text("UPDATE secrets SET account_id = :keep WHERE account_id = ANY(:rest)"),
            {"keep": keep, "rest": rest},
        )
        # Freed first: the natural key includes the number, so the account
        # kept cannot take it while another still holds it.
        await session.execute(text("DELETE FROM accounts WHERE id = ANY(:rest)"), {"rest": rest})
        await session.execute(
            text("UPDATE accounts SET masked_number = :n, updated_at = now() WHERE id = :id"),
            {"id": keep, "n": agreed},
        )
    if not groups:
        print("no split accounts")
    return merged


async def main() -> None:
    settings = get_settings()
    dry_run = "--dry-run" in sys.argv
    init_engine(settings)
    async with (
        get_sessionmaker()() as session,
        session.begin(),
        with_user(session, settings.auth_dev_user_id) as scoped,
    ):
        print("statements read more than once:")
        removed = await _drop_superseded(scoped, dry_run=dry_run)
        print("\naccounts split by a re-read:")
        merged = await _merge_splits(scoped, dry_run=dry_run)
        print(
            f"\n{'would remove' if dry_run else 'removed'} {removed} duplicate transaction(s), "
            f"{'would merge' if dry_run else 'merged'} {merged} account(s)"
        )
        if dry_run:
            print("dry run — nothing written")
    await dispose_engine()


if __name__ == "__main__":
    asyncio.run(main())
