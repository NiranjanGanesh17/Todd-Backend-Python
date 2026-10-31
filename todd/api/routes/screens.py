"""GET /v1/profile and GET /v1/report — whole screens, assembled server-side.

Both read the `security_invoker` views, never the tables directly, so an
unreconciled statement's rows cannot reach a report (§5, §7.3). Every display
string is written here.
"""

from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter, Query
from sqlalchemy import text

from todd.api.deps import SessionDep, SettingsDep, UserIdDep
from todd.api.errors import ApiError
from todd.db.seed.data import UNCATEGORIZED_NAME, UNCATEGORIZED_SLUG
from todd.log import get_logger
from todd.parsers.dates import (
    gap_label,
    last_complete_months,
    month_name,
    preceding_months,
    window_label,
)
from todd.parsers.payee import extract_payee, extract_rail, normalise_payee, row_title
from todd.schemas.api.models import (
    AnalysisWindow,
    BiggestMonth,
    CategoriesResponse,
    CategoryOption,
    CategoryTotal,
    CategoryTransactionsResponse,
    FileTransactionRequest,
    FileTransactionResponse,
    GmailSummary,
    IncomeSource,
    InsightOut,
    LockedAccount,
    MonthlySpend,
    ProfileAccount,
    ProfileResponse,
    ReportResponse,
    ReportTotals,
    ReportWindow,
    TransactionOut,
)
from todd.schemas.enums import (
    INSTITUTION_LABELS,
    AccountStatus,
    AccountType,
    CategoryKind,
    Direction,
    Institution,
    Plan,
)

#: The "Came in" breakdown folds every merchant credit into one line —
#: a refund is not income from the category it came back out of.
REFUNDS_SLUG = "refunds"
REFUNDS_NAME = "Refunds"

logger = get_logger(__name__)
router = APIRouter(prefix="/v1", tags=["screens"])

ALLOWED_WINDOWS: list[AnalysisWindow] = [1, 3, 6, 12]

#: Short forms for a banner: "ICICI card needs a password" reads better than
#: "ICICI Bank credit_card".
_TYPE_SHORT: dict[AccountType, str] = {
    AccountType.SAVINGS: "savings",
    AccountType.CREDIT_CARD: "card",
    AccountType.LOAN: "loan",
}

#: Short bank names for a one-line banner. An explicit map, not string surgery
#: on the full label: stripping " Bank" turns "State Bank of India" into
#: "State of India".
_INSTITUTION_SHORT: dict[Institution, str] = {
    Institution.HDFC: "HDFC",
    Institution.ICICI: "ICICI",
    Institution.SBI: "SBI",
    Institution.AXIS: "Axis",
    Institution.KOTAK: "Kotak",
    Institution.BOB: "Bank of Baroda",
    Institution.UNKNOWN: "Unknown bank",
}

_STATUS_LABELS: dict[AccountStatus, str] = {
    AccountStatus.UP_TO_DATE: "Up to date",
    AccountStatus.NEEDS_PASSWORD: "Needs a password",
    AccountStatus.MISSING_MONTHS: "Missing statements",
}


def institution_label(value: str | None) -> str:
    try:
        return INSTITUTION_LABELS[Institution(value or "unknown")]
    except (ValueError, KeyError):
        return INSTITUTION_LABELS[Institution.UNKNOWN]


def short_institution(value: str | None) -> str:
    """What an account row calls the bank: "HDFC", not "HDFC Bank".

    The row is the bank's name, the account type and the last four digits on
    one line of a phone. "ICICI Bank Credit Card ··1008" wraps onto two and
    pushes the action button out of reach; "ICICI Credit Card ··1008" does
    not, and says the same thing.
    """
    try:
        return _INSTITUTION_SHORT[Institution(value or "unknown")]
    except (ValueError, KeyError):
        return _INSTITUTION_SHORT[Institution.UNKNOWN]


def _short_label(institution: str | None, account_type: str) -> str:
    try:
        name = _INSTITUTION_SHORT[Institution(institution or "unknown")]
    except ValueError:
        name = _INSTITUTION_SHORT[Institution.UNKNOWN]
    try:
        kind = _TYPE_SHORT[AccountType(account_type)]
    except ValueError:
        kind = "account"
    return f"{name} {kind}"


def _locked_label(institution: str | None, pending: int) -> str:
    """Label for statements locked before we know whose account they are.

    Without an institution there is nothing truthful to name, so it says what
    the user can act on instead of inventing a bank.
    """
    if institution and institution != Institution.UNKNOWN.value:
        return _INSTITUTION_SHORT[Institution(institution)]
    return "locked" if pending == 1 else f"{pending} locked"


def _trend_pct(current: int, previous: int) -> float | None:
    """Percent change against the window before, or None without a baseline."""
    if previous <= 0:
        return None
    return round((current - previous) / previous * 100, 1)


# ------------------------------------------------------------------ profile


@router.get("/profile", response_model=ProfileResponse, summary="Plan, accounts and their status")
async def get_profile(
    session: SessionDep, user_id: UserIdDep, settings: SettingsDep
) -> ProfileResponse:
    user = (
        (
            await session.execute(
                text("SELECT email, display_name, plan FROM users WHERE id = :id"),
                {"id": uuid.UUID(user_id)},
            )
        )
        .mappings()
        .one_or_none()
    )

    # The months the report is asking about. An account is only "missing"
    # statements relative to a window: a savings account closed last year is
    # not a gap, it is finished.
    window = await _window_for(session, None)
    keys = last_complete_months(window)

    rows = (
        (
            await session.execute(
                text(
                    """
                SELECT a.id,
                       a.institution::text AS institution,
                       a.type::text        AS type,
                       a.masked_number,
                       EXISTS (
                         SELECT 1 FROM documents d
                         WHERE d.account_id = a.id AND d.status = 'locked'
                       ) AS has_locked,
                       (
                         SELECT array_agg(DISTINCT c.month)
                         FROM v_statement_coverage c
                         WHERE c.account_id = a.id AND c.month = ANY(:keys)
                       ) AS covered
                FROM accounts a
                ORDER BY a.institution, a.type, a.masked_number
                """
                ),
                {"keys": keys},
            )
        )
        .mappings()
        .all()
    )

    accounts: list[ProfileAccount] = []
    for row in rows:
        missing = [key for key in keys if key not in set(row["covered"] or ())]
        # A password beats a gap: an account whose statements are sitting
        # right here unread is not short of statements, it is short of one
        # password, and that is the action to offer.
        if row["has_locked"]:
            status = AccountStatus.NEEDS_PASSWORD
            label = _STATUS_LABELS[status]
        elif missing:
            status = AccountStatus.MISSING_MONTHS
            label = f"Missing {gap_label(missing)}"
        else:
            status = AccountStatus.UP_TO_DATE
            label = _STATUS_LABELS[status]
        accounts.append(
            ProfileAccount(
                id=str(row["id"]),
                institution=short_institution(row["institution"]),
                type=AccountType(row["type"]),
                masked_number=row["masked_number"],
                status=status,
                status_label=label,
            )
        )

    connection = (
        (
            await session.execute(
                text(
                    "SELECT google_email FROM email_connections "
                    "WHERE status = 'active' ORDER BY created_at DESC LIMIT 1"
                )
            )
        )
        .mappings()
        .one_or_none()
    )

    return ProfileResponse(
        display_name=user["display_name"] if user else None,
        email=user["email"] if user else settings.auth_dev_user_email,
        plan=Plan(user["plan"]) if user else Plan.FREE,
        accounts=accounts,
        gmail=GmailSummary(
            connected=connection is not None,
            email=connection["google_email"] if connection else None,
        ),
    )


# ------------------------------------------------------------------- report


async def _window_for(session: SessionDep, requested: int | None) -> AnalysisWindow:
    if requested in ALLOWED_WINDOWS:
        return requested
    saved = (
        await session.execute(text("SELECT analysis_window_months FROM users LIMIT 1"))
    ).scalar_one_or_none()
    return saved if saved in ALLOWED_WINDOWS else 6


@router.get("/report", response_model=ReportResponse, summary="Home — the whole report")
async def get_report(
    session: SessionDep,
    user_id: UserIdDep,
    months: int | None = Query(default=None, description="1, 3, 6 or 12. Omit for the saved one."),
) -> ReportResponse:
    month_count = await _window_for(session, months)
    keys = last_complete_months(month_count)

    user = (
        (
            await session.execute(
                text("SELECT display_name FROM users WHERE id = :id"), {"id": uuid.UUID(user_id)}
            )
        )
        .mappings()
        .one_or_none()
    )

    monthly = (
        (
            await session.execute(
                text(
                    "SELECT month, COALESCE(spent_paise, 0) AS spent, "
                    "       COALESCE(received_paise, 0) AS received "
                    "FROM v_monthly_spend WHERE month = ANY(:keys) ORDER BY month"
                ),
                {"keys": keys},
            )
        )
        .mappings()
        .all()
    )

    by_month: dict[str, dict[str, int]] = {
        r["month"]: {"spent": int(r["spent"]), "received": int(r["received"])} for r in monthly
    }
    monthly_spend = [
        MonthlySpend(month=k, total_paise=by_month.get(k, {}).get("spent", 0)) for k in keys
    ]
    spent = sum(m.total_paise for m in monthly_spend)
    money_in = sum(by_month.get(k, {}).get("received", 0) for k in keys)

    # Both windows in one pass: the one on screen, and the equally long one
    # before it that every trend is measured against. A category with no
    # history gets no trend at all — "up 100%" from nothing is not a fact
    # about spending, it is a fact about when the statements start.
    previous = preceding_months(keys)
    categories = [
        CategoryTotal(
            slug=r["category_slug"] or UNCATEGORIZED_SLUG,
            name=r["category_name"] or UNCATEGORIZED_NAME,
            total_paise=int(r["amount_paise"]),
            trend_pct=_trend_pct(int(r["amount_paise"]), int(r["previous_paise"] or 0)),
            txn_count=int(r["txn_count"] or 0),
        )
        for r in (
            await session.execute(
                text(
                    "SELECT category_slug, category_name, "
                    "       COALESCE(SUM(amount_paise) FILTER (WHERE month = ANY(:keys)), 0) "
                    "         AS amount_paise, "
                    "       COALESCE(SUM(txn_count) FILTER (WHERE month = ANY(:keys)), 0) "
                    "         AS txn_count, "
                    "       COALESCE(SUM(amount_paise) FILTER (WHERE month = ANY(:previous)), 0) "
                    "         AS previous_paise "
                    "FROM v_category_spend "
                    "WHERE month = ANY(:keys) OR month = ANY(:previous) "
                    "GROUP BY category_slug, category_name "
                    "HAVING COALESCE(SUM(amount_paise) FILTER (WHERE month = ANY(:keys)), 0) > 0 "
                    "ORDER BY 3 DESC"
                ),
                {"keys": keys, "previous": previous},
            )
        )
        .mappings()
        .all()
    ]

    # A locked document usually has NO account: the account is not known until
    # the PDF opens. Keying the banner on accounts alone means someone with
    # twenty password-protected statements is told we found nothing and given
    # nothing to click, which is exactly when the banner matters most.
    # Where "Came in" came from. Reads the publishable view like everything
    # else, so an unreconciled statement contributes nothing here either.
    #
    # A credit that lands in a *spend* category is a refund — Swiggy giving
    # money back is not income from food delivery, and listing it that way
    # reads as nonsense. They fold into one "Refunds" line.
    income_rows = (
        (
            await session.execute(
                text(
                    "SELECT c.slug, c.name, c.kind::text AS kind, "
                    "       SUM(t.amount_paise) AS total "
                    "FROM v_publishable_transactions t "
                    "LEFT JOIN categories c ON c.id = t.category_id "
                    "WHERE to_char(t.txn_date, 'YYYY-MM') = ANY(:keys) "
                    "  AND t.direction = 'credit' AND NOT t.is_transfer "
                    "GROUP BY c.slug, c.name, c.kind ORDER BY 4 DESC"
                ),
                {"keys": keys},
            )
        )
        .mappings()
        .all()
    )

    income_sources: list[IncomeSource] = []
    refunds = 0
    for row in income_rows:
        if row["kind"] == "spend":
            refunds += int(row["total"])
            continue
        income_sources.append(
            IncomeSource(
                slug=row["slug"] or "income_other",
                name=row["name"] or "Other income",
                total_paise=int(row["total"]),
                trend_pct=None,
            )
        )
    if refunds:
        income_sources.append(
            IncomeSource(slug="refunds", name="Refunds", total_paise=refunds, trend_pct=None)
        )
    income_sources.sort(key=lambda source: source.total_paise, reverse=True)

    locked = [
        LockedAccount(
            id=str(r["id"]),
            label=(
                _short_label(r["institution"], r["type"])
                if r["type"]
                else _locked_label(r["institution"], int(r["pending"]))
            ),
        )
        for r in (
            await session.execute(
                text(
                    """
                    SELECT a.id,
                           a.institution::text AS institution,
                           a.type::text        AS type,
                           1                   AS pending
                    FROM accounts a
                    WHERE EXISTS (SELECT 1 FROM documents d
                                  WHERE d.account_id = a.id AND d.status = 'locked')
                    UNION ALL
                    SELECT DISTINCT ON (d.institution)
                           d.id,
                           d.institution::text AS institution,
                           NULL                AS type,
                           count(*) OVER (PARTITION BY d.institution) AS pending
                    FROM documents d
                    WHERE d.status = 'locked' AND d.account_id IS NULL
                    """
                )
            )
        )
        .mappings()
        .all()
    ]

    worth_a_look = [
        InsightOut(
            id=str(r["id"]),
            type=r["type"],
            title=r["title"],
            body=r["body"],
            payload=dict(r["payload"] or {}),
            period=r["period"] or "",
            status=r["status"],
        )
        for r in (
            await session.execute(
                text(
                    "SELECT id, type::text AS type, title, body, payload, period, "
                    "       status::text AS status FROM insights "
                    "WHERE status <> 'dismissed' ORDER BY created_at DESC LIMIT 6"
                )
            )
        )
        .mappings()
        .all()
    ]

    biggest: BiggestMonth | None = None
    if spent > 0:
        top = max(monthly_spend, key=lambda m: m.total_paise)
        if top.total_paise > 0:
            # The chart already draws how much bigger it was; the caption names
            # *which* month, so the highlight never rests on colour alone.
            caption = f"{month_name(top.month)} was your biggest month."
            biggest = BiggestMonth(month=top.month, caption=caption)

    saved_pct: float | None = None
    if money_in > 0:
        saved_pct = round((money_in - spent) / money_in * 100, 1)

    return ReportResponse(
        display_name=user["display_name"] if user else None,
        has_data=bool(monthly or categories),
        window=ReportWindow(months=keys, label=window_label(keys), month_count=month_count),
        available_windows=ALLOWED_WINDOWS,
        locked_accounts=locked,
        totals=ReportTotals(
            spent_paise=spent, money_in_paise=money_in, saved_pct_of_income=saved_pct
        ),
        monthly_spend=monthly_spend,
        income_sources=income_sources,
        biggest_month=biggest,
        categories=categories,
        worth_a_look=worth_a_look,
    )


# ---------------------------------------------- category drill-down (new)


#: Not a Node route. The web app has no screen for this yet, so it is additive
#: — `openapi.json` gains an operation the Node spec does not have, which is
#: expected for new functionality rather than drift.
@router.get(
    "/categories/{slug}/transactions",
    response_model=CategoryTransactionsResponse,
    summary="The transactions behind one category bar",
)
async def category_transactions(
    slug: str,
    session: SessionDep,
    user_id: UserIdDep,
    months: int | None = Query(default=None, description="1, 3, 6 or 12."),
    direction: Literal["debit", "credit"] = Query(
        default="debit", description="debit for spend, credit for income"
    ),
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
) -> CategoryTransactionsResponse:
    """Rows behind a bar on the report, for the window it was drawn for.

    Reads `v_publishable_transactions`, so a statement that failed the
    reconciliation gate contributes nothing here either.

    `direction` picks which side of the ledger: `debit` mirrors
    `v_category_spend` exactly, `credit` mirrors the "Came in" breakdown. A
    drill-down whose total differs from the bar that opened it is worse than
    no drill-down at all, so the filters have to match on both sides.
    """
    month_count = await _window_for(session, months)
    keys = last_complete_months(month_count)
    window = ReportWindow(months=keys, label=window_label(keys), month_count=month_count)

    name = (
        await session.execute(
            text("SELECT name FROM categories WHERE slug = :slug"), {"slug": slug}
        )
    ).scalar_one_or_none()

    # Two synthetic buckets, neither a row in `categories`: "no category at
    # all", and "a credit that landed in a spend category", which is a refund.
    if name is None:
        if slug == UNCATEGORIZED_SLUG:
            name = UNCATEGORIZED_NAME
        elif slug == REFUNDS_SLUG:
            name = REFUNDS_NAME
        else:
            raise ApiError(404, "not_found", f"No category called {slug!r}.")

    if slug == UNCATEGORIZED_SLUG:
        match = "t.category_id IS NULL"
    elif slug == REFUNDS_SLUG:
        match = "c.kind = 'spend'"
    else:
        match = "c.slug = :slug"

    params: dict[str, object] = {
        "keys": keys,
        "limit": limit,
        "offset": offset,
        "direction": direction,
    }
    if slug not in (UNCATEGORIZED_SLUG, REFUNDS_SLUG):
        params["slug"] = slug

    base = f"""
        FROM v_publishable_transactions t
        LEFT JOIN categories c ON c.id = t.category_id
        LEFT JOIN merchants  m ON m.id = t.merchant_id
        LEFT JOIN accounts   a ON a.id = t.account_id
        WHERE to_char(t.txn_date, 'YYYY-MM') = ANY(:keys)
          AND NOT t.is_transfer
          AND t.direction = :direction
          AND {match}
    """

    summary = (
        (
            await session.execute(
                text(f"SELECT count(*) AS n, COALESCE(SUM(t.amount_paise), 0) AS total {base}"),
                params,
            )
        )
        .mappings()
        .one()
    )

    rows = (
        (
            await session.execute(
                text(
                    "SELECT t.id, t.txn_date, t.amount_paise, t.direction::text AS direction, "
                    "       t.raw_description, t.counterparty_name, t.counterparty_vpa, "
                    "       t.category_source, t.category_confidence, t.is_transfer, "
                    "       m.normalized_name AS merchant_name, "
                    "       c.slug AS category_slug, c.name AS category_name, "
                    "       a.institution::text AS institution, a.type::text AS account_type, "
                    "       a.masked_number "
                    f"{base} "
                    "ORDER BY t.txn_date DESC, t.amount_paise DESC "
                    "LIMIT :limit OFFSET :offset"
                ),
                params,
            )
        )
        .mappings()
        .all()
    )

    return CategoryTransactionsResponse(
        slug=slug,
        name=name,
        window=window,
        total_paise=int(summary["total"]),
        count=int(summary["n"]),
        transactions=[
            TransactionOut(
                id=str(r["id"]),
                txn_date=r["txn_date"].isoformat(),
                amount_paise=int(r["amount_paise"]),
                direction=Direction(r["direction"]),
                raw_description=r["raw_description"],
                title=row_title(
                    description=r["raw_description"],
                    merchant_name=r["merchant_name"],
                    counterparty_name=r["counterparty_name"],
                    counterparty_vpa=r["counterparty_vpa"],
                ),
                rail=extract_rail(r["raw_description"]),
                account_masked=r["masked_number"],
                merchant_name=r["merchant_name"],
                counterparty_name=r["counterparty_name"],
                counterparty_vpa=r["counterparty_vpa"],
                category_slug=r["category_slug"] or UNCATEGORIZED_SLUG,
                category_name=r["category_name"] or UNCATEGORIZED_NAME,
                category_source=r["category_source"],
                category_confidence=(
                    float(r["category_confidence"])
                    if r["category_confidence"] is not None
                    else None
                ),
                is_transfer=r["is_transfer"],
                account_label=_short_label(r["institution"], r["account_type"] or ""),
            )
            for r in rows
        ],
    )


# ------------------------------------------------- filing a transaction


@router.get("/categories", response_model=CategoriesResponse, summary="Categories to file under")
async def list_categories(session: SessionDep, user_id: UserIdDep) -> CategoriesResponse:
    """Every category a transaction can be moved into, in display order."""
    rows = (
        (
            await session.execute(
                text(
                    "SELECT slug, name, kind::text AS kind FROM categories "
                    "ORDER BY sort_order, name"
                )
            )
        )
        .mappings()
        .all()
    )
    return CategoriesResponse(
        categories=[
            CategoryOption(slug=r["slug"], name=r["name"], kind=CategoryKind(r["kind"]))
            for r in rows
        ]
    )


@router.post(
    "/transactions/{transaction_id}/category",
    response_model=FileTransactionResponse,
    summary="File a transaction under a category",
)
async def file_transaction(
    transaction_id: str,
    body: FileTransactionRequest,
    session: SessionDep,
    user_id: UserIdDep,
) -> FileTransactionResponse:
    """Move one transaction, and optionally every future one from the payee.

    A correction is the top of the ladder — confidence 1.0, `category_source`
    of `user_rule` — so no automatic rung and no model run can overwrite it.
    Filing under the same payee is what makes sorting compound rather than
    being busywork: one decision covers every month that follows.
    """
    try:
        txn_id = uuid.UUID(transaction_id)
    except ValueError as exc:
        raise ApiError(400, "bad_request", "That is not a transaction id.") from exc

    category = (
        (
            await session.execute(
                text("SELECT id, name FROM categories WHERE slug = :slug"),
                {"slug": body.category_slug},
            )
        )
        .mappings()
        .one_or_none()
    )
    if category is None:
        raise ApiError(404, "not_found", f"No category called {body.category_slug!r}.")

    txn = (
        (
            await session.execute(
                text(
                    "SELECT id, raw_description, counterparty_vpa FROM transactions WHERE id = :id"
                ),
                {"id": txn_id},
            )
        )
        .mappings()
        .one_or_none()
    )
    if txn is None:
        raise ApiError(404, "not_found", "No such transaction.")

    affected = 1
    await session.execute(
        text(
            "UPDATE transactions SET category_id = :cid, category_source = 'user_rule', "
            "  category_confidence = 1.0, is_transfer = :transfer, updated_at = now() "
            "WHERE id = :id"
        ),
        {
            "id": txn_id,
            "cid": category["id"],
            "transfer": body.category_slug == "transfers_own",
        },
    )

    if body.apply_to_future:
        payee = normalise_payee(extract_payee(txn["raw_description"] or ""))
        vpa = txn["counterparty_vpa"]

        if payee:
            # Cached so no later rung, and no model run, re-decides it.
            await session.execute(
                text(
                    "INSERT INTO user_payee_labels (user_id, payee, category_id, source, "
                    "  confidence) VALUES (:uid, :payee, :cid, 'user_rule', 1.0) "
                    "ON CONFLICT (user_id, payee) DO UPDATE SET "
                    "  category_id = EXCLUDED.category_id, source = 'user_rule', "
                    "  confidence = 1.0, updated_at = now()"
                ),
                {"uid": uuid.UUID(user_id), "payee": payee[:160], "cid": category["id"]},
            )
        if vpa:
            await session.execute(
                text(
                    "INSERT INTO user_rules (user_id, match_kind, match_value, category_id) "
                    "VALUES (:uid, 'vpa', :value, :cid) "
                    "ON CONFLICT (user_id, match_kind, match_value) DO UPDATE SET "
                    "  category_id = EXCLUDED.category_id"
                ),
                {"uid": uuid.UUID(user_id), "value": vpa[:160], "cid": category["id"]},
            )

        # Apply it to everything already in the ledger from the same payee.
        if vpa or payee:
            result = await session.execute(
                text(
                    "UPDATE transactions SET category_id = :cid, "
                    "  category_source = 'user_rule', category_confidence = 1.0, "
                    "  is_transfer = :transfer, updated_at = now() "
                    # The casts are required: Postgres cannot infer a
                    # parameter's type from `IS NOT NULL` alone, and refuses
                    # the statement with AmbiguousParameter.
                    "WHERE id <> :id AND category_source <> 'user_rule' "
                    "  AND ((:vpa)::text IS NOT NULL AND counterparty_vpa = (:vpa)::text "
                    "       OR (:payee)::text IS NOT NULL "
                    "          AND counterparty_name ILIKE (:like)::text)"
                ),
                {
                    "id": txn_id,
                    "cid": category["id"],
                    "vpa": vpa,
                    "payee": payee,
                    "like": payee or "",
                    "transfer": body.category_slug == "transfers_own",
                },
            )
            affected += getattr(result, "rowcount", 0) or 0

    logger.info(
        "transaction_filed",
        category=body.category_slug,
        apply_to_future=body.apply_to_future,
        affected=affected,
    )
    return FileTransactionResponse(
        id=transaction_id,
        category_slug=body.category_slug,
        category_name=category["name"],
        affected=affected,
    )
