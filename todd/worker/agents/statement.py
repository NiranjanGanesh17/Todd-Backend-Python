"""PDF -> ledger. The orchestrator.

The order below is the one the TypeScript version arrived at, and it matters:
the document is claimed before any work starts, the password ladder runs only
on an encryption error, cost is recorded per model call, and the write
converges rather than appends.

Nothing reaches the ledger unreconciled. A statement that fails the gate is
stored as `needs_review` with the shortfall, and its rows stay invisible to the
report because `v_publishable_transactions` filters them out.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from todd.config import Settings
from todd.log import get_logger
from todd.parsers.dedupe import fingerprint
from todd.parsers.institution import detect_account
from todd.parsers.pdf.extract import (
    Document,
    PdfEncryptedError,
    PdfUnreadableError,
    extract_document,
    is_encrypted,
)
from todd.parsers.reconcile import reconcile
from todd.parsers.table.extract import ExtractedRow, extract_table
from todd.schemas.enums import (
    AccountType,
    DocumentStatus,
    Institution,
    ParseEngine,
    ParseStrategy,
)
from todd.worker import progress
from todd.worker.lib.crypto import Sealed, try_unseal

logger = get_logger(__name__)

#: §7.1 — a hard cap, so a bad hint cannot turn into a brute-force loop.
MAX_PASSWORD_ATTEMPTS = 25


@dataclass(slots=True)
class ParseAttempt:
    strategy: ParseStrategy
    ok: bool
    rows: int
    reason: str | None = None


@dataclass(slots=True)
class StatementResult:
    status: DocumentStatus
    rows_written: int = 0
    reconciled: bool = False
    difference_paise: int | None = None
    engine: ParseEngine | None = None
    attempts: list[ParseAttempt] = field(default_factory=list)
    error: str | None = None
    statement_id: uuid.UUID | None = None
    account_id: uuid.UUID | None = None


# ----------------------------------------------------------- password ladder


async def password_candidates(
    session: AsyncSession,
    *,
    user_id: str,
    settings: Settings,
    typed: str | None,
) -> list[str]:
    """Passwords to try, cheapest and most likely first.

    The account is not known until the PDF opens, so every saved secret is a
    candidate. A secret sealed under a rotated key is skipped with a warning,
    never a crash.

    Generated guesses from name and date of birth require unrevoked
    `password_guessing` consent, checked at the point of use.
    """
    candidates: list[str] = []
    if typed:
        candidates.append(typed)

    rows = (
        (
            await session.execute(
                text(
                    "SELECT ciphertext, nonce, key_id FROM secrets "
                    "WHERE kind = 'pdf_password' ORDER BY created_at DESC"
                )
            )
        )
        .mappings()
        .all()
    )
    for row in rows:
        opened = try_unseal(
            Sealed(ciphertext=row["ciphertext"], nonce=row["nonce"], key_id=row["key_id"]),
            master_key=settings.encryption_master_key or "",
        )
        if opened and opened not in candidates:
            candidates.append(opened)

    consented = (
        await session.execute(
            text(
                "SELECT 1 FROM user_consents WHERE purpose = 'password_guessing' "
                "AND revoked_at IS NULL LIMIT 1"
            )
        )
    ).scalar_one_or_none()

    if consented:
        profile = (
            (
                await session.execute(
                    text("SELECT display_name, date_of_birth FROM users WHERE id = :id"),
                    {"id": uuid.UUID(user_id)},
                )
            )
            .mappings()
            .one_or_none()
        )
        if profile:
            for guess in generate_guesses(profile["display_name"], profile["date_of_birth"]):
                if guess not in candidates:
                    candidates.append(guess)

    return candidates[:MAX_PASSWORD_ATTEMPTS]


def generate_guesses(display_name: str | None, dob: dt.date | None) -> list[str]:
    """The patterns Indian banks actually use for statement passwords.

    Only ever called with unrevoked `password_guessing` consent.
    """
    guesses: list[str] = []
    first = (display_name or "").strip().split(" ")[0]
    if dob:
        guesses += [
            dob.strftime("%d%m%Y"),
            dob.strftime("%d%m%y"),
            dob.strftime("%d-%m-%Y"),
            dob.strftime("%Y%m%d"),
        ]
        if first:
            lower4 = first[:4].lower()
            guesses += [
                f"{lower4}{dob.strftime('%d%m')}",
                f"{first[:4].upper()}{dob.strftime('%d%m')}",
                f"{lower4}{dob.strftime('%d%m%Y')}",
            ]
    return guesses


def open_with_ladder(
    data: bytes,
    candidates: list[str],
    *,
    on_page: Callable[[int, int], None] | None = None,
) -> tuple[Document | None, str | None, int]:
    """Try no password first, then each candidate. Returns (doc, password, tries).

    Trying without a password first matters: most statements are not encrypted
    at all, and the ladder should cost nothing for them.
    """
    if not is_encrypted(data):
        return extract_document(data, on_page=on_page), None, 0

    for index, candidate in enumerate(candidates, start=1):
        try:
            return extract_document(data, candidate, on_page=on_page), candidate, index
        except PdfEncryptedError:
            continue
    return None, None, len(candidates)


# ------------------------------------------------------------------- writing


async def resolve_account(
    session: AsyncSession,
    *,
    user_id: str,
    institution: Institution,
    account_type: AccountType,
    masked_number: str | None,
    institution_name: str | None = None,
    document_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """Find or create the account on its natural key.

    §7.1 step 7 — this is how an account appears on Profile without the user
    ever typing an account number.

    A document that already belongs to an account keeps it. The natural key
    includes the masked number, so a re-read that finally manages to see one
    resolved to a *different* account than the first read did — and because a
    transaction's fingerprint is scoped to its account, every row was written
    again under the new one while the old rows stayed in the report. One
    statement read twice became two accounts and two of every transaction.
    What the re-read learns is used to fill in the account it already has,
    never to move the statement to a new one.
    """
    if document_id is not None:
        owned = (
            await session.execute(
                text("SELECT account_id FROM documents WHERE id = :id"), {"id": document_id}
            )
        ).scalar_one_or_none()
        if owned:
            await session.execute(
                text(
                    "UPDATE accounts SET "
                    "  institution = CASE WHEN institution = 'unknown' THEN CAST(:inst AS "
                    "    institution) ELSE institution END, "
                    "  masked_number = COALESCE(masked_number, :masked), "
                    "  institution_name = COALESCE(institution_name, :inst_name), "
                    "  updated_at = now() "
                    "WHERE id = :id"
                ),
                {
                    "id": owned,
                    "inst": institution.value,
                    "masked": masked_number,
                    "inst_name": institution_name,
                },
            )
            return uuid.UUID(str(owned))

    existing = (
        await session.execute(
            text(
                "SELECT id FROM accounts WHERE institution = :inst AND type = :type "
                "AND masked_number IS NOT DISTINCT FROM :masked"
            ),
            {"inst": institution.value, "type": account_type.value, "masked": masked_number},
        )
    ).scalar_one_or_none()
    if existing:
        return uuid.UUID(str(existing))

    created = (
        await session.execute(
            text(
                "INSERT INTO accounts "
                "(user_id, institution, type, masked_number, institution_name, source) "
                "VALUES (:uid, :inst, :type, :masked, :inst_name, 'detected') RETURNING id"
            ),
            {
                "uid": uuid.UUID(user_id),
                "inst": institution.value,
                "type": account_type.value,
                "masked": masked_number,
                "inst_name": institution_name,
            },
        )
    ).scalar_one()
    return uuid.UUID(str(created))


async def converge_transactions(
    session: AsyncSession,
    *,
    user_id: str,
    account_id: uuid.UUID,
    statement_id: uuid.UUID,
    rows: list[ExtractedRow],
) -> int:
    """Make the ledger match this statement exactly, in one transaction.

    §8.11 — compute the target fingerprint set, delete the rows this statement
    no longer claims, then insert what is missing. "Insert what's new" alone
    can never remove a stale row, so a corrected re-parse would leave the
    original mistake in the ledger forever.
    """
    targets = {
        fingerprint(
            account_id=str(account_id),
            txn_date=row.txn_date,
            amount_paise=row.amount_paise,
            direction=row.direction,
            description=row.description,
        ): row
        for row in rows
    }

    await session.execute(
        text(
            "DELETE FROM transactions WHERE statement_id = :sid AND NOT (fingerprint = ANY(:keep))"
        ),
        {"sid": statement_id, "keep": list(targets) or [""]},
    )

    written = 0
    for fp, row in targets.items():
        result = await session.execute(
            text(
                "INSERT INTO transactions "
                "(user_id, account_id, statement_id, source, txn_date, amount_paise, "
                " direction, raw_description, fingerprint) "
                "VALUES (:uid, :aid, :sid, 'statement', :d, :amt, :dir, :desc, :fp) "
                "ON CONFLICT (user_id, fingerprint) DO UPDATE SET "
                "  statement_id = EXCLUDED.statement_id, "
                "  amount_paise = EXCLUDED.amount_paise, "
                "  direction    = EXCLUDED.direction, "
                "  updated_at   = now() "
                "RETURNING id"
            ),
            {
                "uid": uuid.UUID(user_id),
                "aid": account_id,
                "sid": statement_id,
                "d": row.txn_date,
                "amt": row.amount_paise,
                "dir": row.direction,
                "desc": row.description[:4000],
                "fp": fp,
            },
        )
        if result.scalar_one_or_none():
            written += 1
    return written


async def upsert_statement(
    session: AsyncSession,
    *,
    user_id: str,
    account_id: uuid.UUID,
    document_id: uuid.UUID,
    period_start: dt.date,
    period_end: dt.date,
    opening_paise: int | None,
    closing_paise: int | None,
    reconciled: bool,
    difference_paise: int | None,
    engine: ParseEngine,
) -> uuid.UUID:
    """One statement row per document. A re-parse updates it in place.

    Keyed on the document first, and only then on the account and period.
    Keyed on the period alone, a re-read whose rows start a day earlier — or
    one that landed on a different account — inserted a second statement and
    left the first behind, still reconciled and still feeding the report. The
    document is the thing that was read, so it is the thing that owns the row.
    """
    mine, *extra = (
        (
            await session.execute(
                text("SELECT id FROM statements WHERE document_id = :did ORDER BY created_at"),
                {"did": document_id},
            )
        )
        .scalars()
        .all()
    ) or [None]

    if extra:
        # A ledger written before this was keyed on the document can hold
        # several statements for one file — that was the bug. Their rows are
        # the duplicates on screen, and `converge_transactions` is about to
        # write the real set onto the one being kept.
        #
        # Deleted outright, not left to the foreign key: it is ON DELETE SET
        # NULL, and a transaction with no statement is *published* — the
        # duplicates would survive the statement that explains them.
        await session.execute(
            text("DELETE FROM transactions WHERE statement_id = ANY(:ids)"), {"ids": extra}
        )
        await session.execute(text("DELETE FROM statements WHERE id = ANY(:ids)"), {"ids": extra})
        logger.info(
            "collapsed_duplicate_statements",
            document_id=str(document_id),
            removed=len(extra),
        )

    if mine is not None:
        await session.execute(
            text(
                "UPDATE statements SET account_id = :aid, period_start = :ps, period_end = :pe, "
                "  opening_balance = :ob, closing_balance = :cb, reconciled = :rec, "
                "  reconcile_difference = :diff, parse_engine = :eng, updated_at = now() "
                "WHERE id = :id"
            ),
            {
                "id": mine,
                "aid": account_id,
                "ps": period_start,
                "pe": period_end,
                "ob": opening_paise,
                "cb": closing_paise,
                "rec": reconciled,
                "diff": difference_paise,
                "eng": engine.value,
            },
        )
        return uuid.UUID(str(mine))

    row = (
        await session.execute(
            text(
                "INSERT INTO statements (user_id, account_id, document_id, period_start, "
                "  period_end, opening_balance, closing_balance, reconciled, "
                "  reconcile_difference, parse_engine) "
                "VALUES (:uid, :aid, :did, :ps, :pe, :ob, :cb, :rec, :diff, :eng) "
                "ON CONFLICT (account_id, period_start, period_end) DO UPDATE SET "
                "  document_id = EXCLUDED.document_id, "
                "  opening_balance = EXCLUDED.opening_balance, "
                "  closing_balance = EXCLUDED.closing_balance, "
                "  reconciled = EXCLUDED.reconciled, "
                "  reconcile_difference = EXCLUDED.reconcile_difference, "
                "  parse_engine = EXCLUDED.parse_engine, "
                "  updated_at = now() "
                "RETURNING id"
            ),
            {
                "uid": uuid.UUID(user_id),
                "aid": account_id,
                "did": document_id,
                "ps": period_start,
                "pe": period_end,
                "ob": opening_paise,
                "cb": closing_paise,
                "rec": reconciled,
                "diff": difference_paise,
                "eng": engine.value,
            },
        )
    ).scalar_one()
    return uuid.UUID(str(row))


# ------------------------------------------------------------------- the run


async def process_document(
    session: AsyncSession,
    *,
    document_id: uuid.UUID,
    user_id: str,
    settings: Settings,
    data: bytes,
    typed_password: str | None = None,
    remember_password: bool = False,
    institution: Institution = Institution.UNKNOWN,
    account_type: AccountType = AccountType.SAVINGS,
) -> StatementResult:
    """Run one document from bytes to ledger.

    Whatever happens, the page counter is taken down on the way out: a stale
    "Page 3 of 6" against a document that failed ten minutes ago is worse than
    no number at all.
    """
    try:
        return await _run(
            session,
            document_id=document_id,
            user_id=user_id,
            settings=settings,
            data=data,
            typed_password=typed_password,
            remember_password=remember_password,
            institution=institution,
            account_type=account_type,
        )
    finally:
        progress.clear(str(document_id))


async def _run(
    session: AsyncSession,
    *,
    document_id: uuid.UUID,
    user_id: str,
    settings: Settings,
    data: bytes,
    typed_password: str | None,
    remember_password: bool,
    institution: Institution,
    account_type: AccountType,
) -> StatementResult:
    log = logger.bind(document_id=str(document_id))

    await session.execute(
        text("UPDATE documents SET status = 'parsing', updated_at = now() WHERE id = :id"),
        {"id": document_id},
    )

    candidates = await password_candidates(
        session, user_id=user_id, settings=settings, typed=typed_password
    )

    # Published page by page while the parse runs, so the upload screen can
    # say "Page 3 of 6" instead of spinning at nothing.
    def _page(page: int, total: int) -> None:
        progress.advance(str(document_id), page, total)

    # In a thread: pdfplumber is synchronous and a twelve-page statement holds
    # the loop for seconds. Run inline, the upload screen cannot even ask how
    # far it has got until the parse it is asking about has finished.
    try:
        document, used_password, tries = await asyncio.to_thread(
            open_with_ladder, data, candidates, on_page=_page
        )
    except PdfUnreadableError as exc:
        await _fail(session, document_id, f"not a readable PDF: {exc}")
        return StatementResult(status=DocumentStatus.FAILED, error=str(exc))

    if document is None:
        await session.execute(
            text(
                "UPDATE documents SET status = 'locked', "
                "unlock_attempts = unlock_attempts + :n, updated_at = now() WHERE id = :id"
            ),
            {"id": document_id, "n": max(tries, 1)},
        )
        log.info("document_locked", attempts=tries)
        return StatementResult(status=DocumentStatus.LOCKED, error="needs a password")

    # §12 — the password is discarded unless the user asked us to remember it.
    if used_password and remember_password:
        await _remember(session, user_id=user_id, settings=settings, password=used_password)

    # Who this belongs to, read off the page rather than assumed. The caller's
    # hint — the Gmail sender's bank — stands in only where the header is
    # silent. Without this every statement resolves to the same "unknown
    # savings" account and Profile shows one row for four real accounts.
    hint = detect_account(
        [line.text for line in document.pages[0].lines] if document.pages else [],
        fallback=institution,
        fallback_type=account_type,
    )
    institution = hint.institution
    account_type = hint.account_type

    result = extract_table(document)
    attempts = [
        ParseAttempt(
            strategy=ParseStrategy.TABLE_EXTRACT,
            ok=result.ok,
            rows=len(result.rows),
            reason=result.reason,
        )
    ]

    if not result.rows:
        await _fail(session, document_id, result.reason or "no transactions found")
        return StatementResult(status=DocumentStatus.FAILED, attempts=attempts, error=result.reason)

    gate = reconcile(
        result.rows,
        account_type=account_type,
        opening_paise=result.opening_printed_paise,
        closing_paise=result.closing_printed_paise,
    )

    period_start = min(r.txn_date for r in result.rows)
    period_end = max(r.txn_date for r in result.rows)

    account_id = await resolve_account(
        session,
        user_id=user_id,
        institution=institution,
        account_type=account_type,
        masked_number=hint.masked_number,
        institution_name=hint.institution_name,
        document_id=document_id,
    )
    statement_id = await upsert_statement(
        session,
        user_id=user_id,
        account_id=account_id,
        document_id=document_id,
        period_start=period_start,
        period_end=period_end,
        opening_paise=result.opening_printed_paise,
        closing_paise=result.closing_printed_paise,
        reconciled=gate.reconciled,
        difference_paise=gate.difference_paise,
        engine=ParseEngine.TABLE_EXTRACT,
    )
    written = await converge_transactions(
        session,
        user_id=user_id,
        account_id=account_id,
        statement_id=statement_id,
        rows=result.rows,
    )

    # Label what was just written. Rungs 1-7 are pure rules: no network, no
    # model, no cost, so there is no reason to defer them to a queue.
    from todd.worker.agents.enrich import enrich_user

    await enrich_user(session, user_id=user_id, settings=settings)

    status = DocumentStatus.DONE if gate.reconciled else DocumentStatus.NEEDS_REVIEW
    await session.execute(
        text(
            "UPDATE documents SET status = :st, parse_engine = 'table_extract', "
            "account_id = :aid, error = :err, processed_at = now(), updated_at = now() "
            "WHERE id = :id"
        ),
        {
            "id": document_id,
            "st": status.value,
            "aid": account_id,
            "err": None if gate.reconciled else gate.reason,
        },
    )

    log.info(
        "document_parsed",
        status=status.value,
        rows=written,
        reconciled=gate.reconciled,
        chain=gate.chain.verdict,
    )
    return StatementResult(
        status=status,
        rows_written=written,
        reconciled=gate.reconciled,
        difference_paise=gate.difference_paise,
        engine=ParseEngine.TABLE_EXTRACT,
        attempts=attempts,
        error=None if gate.reconciled else gate.reason,
        statement_id=statement_id,
        account_id=account_id,
    )


async def _fail(session: AsyncSession, document_id: uuid.UUID, reason: str) -> None:
    await session.execute(
        text(
            "UPDATE documents SET status = 'failed', error = :err, updated_at = now() "
            "WHERE id = :id"
        ),
        {"id": document_id, "err": reason[:500]},
    )


async def _remember(
    session: AsyncSession, *, user_id: str, settings: Settings, password: str
) -> None:
    from todd.worker.lib.crypto import seal

    sealed = seal(
        password,
        master_key=settings.encryption_master_key or "",
        key_id=settings.encryption_key_id,
    )
    await session.execute(
        text(
            "INSERT INTO secrets (user_id, account_id, kind, ciphertext, nonce, key_id) "
            "VALUES (:uid, NULL, 'pdf_password', :ct, :nonce, :kid) "
            "ON CONFLICT (user_id, account_id, kind) DO UPDATE SET "
            "  ciphertext = EXCLUDED.ciphertext, nonce = EXCLUDED.nonce, "
            "  key_id = EXCLUDED.key_id"
        ),
        {
            "uid": uuid.UUID(user_id),
            "ct": sealed.ciphertext,
            "nonce": sealed.nonce,
            "kid": sealed.key_id,
        },
    )
