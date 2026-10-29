"""GET /v1/profile and GET /v1/report — whole screens, assembled server-side.

Both read the `security_invoker` views, never the tables directly, so an
unreconciled statement's rows cannot reach a report (§5, §7.3). Every display
string is written here.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Query
from sqlalchemy import text

from todd.api.deps import SessionDep, SettingsDep, UserIdDep
from todd.parsers.dates import last_complete_months, window_label
from todd.schemas.api.models import (
    AnalysisWindow,
    BiggestMonth,
    CategoryTotal,
    GmailSummary,
    InsightOut,
    LockedAccount,
    MonthlySpend,
    ProfileAccount,
    ProfileResponse,
    ReportResponse,
    ReportTotals,
    ReportWindow,
)
from todd.schemas.enums import (
    INSTITUTION_LABELS,
    AccountStatus,
    AccountType,
    Institution,
    Plan,
)

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


def _rupees(paise: int) -> str:
    """Indian grouping, whole rupees. `Decimal` only to render (§8.2)."""
    whole = abs(paise) // 100
    digits = str(whole)
    if len(digits) <= 3:
        grouped = digits
    else:
        head, tail = digits[:-3], digits[-3:]
        parts: list[str] = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        grouped = ",".join([*parts, tail])
    return f"₹{grouped}"


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
                       ) AS has_locked
                FROM accounts a
                ORDER BY a.institution, a.type, a.masked_number
                """
                )
            )
        )
        .mappings()
        .all()
    )

    accounts: list[ProfileAccount] = []
    for row in rows:
        status = AccountStatus.NEEDS_PASSWORD if row["has_locked"] else AccountStatus.UP_TO_DATE
        accounts.append(
            ProfileAccount(
                id=str(row["id"]),
                institution=institution_label(row["institution"]),
                type=AccountType(row["type"]),
                masked_number=row["masked_number"],
                status=status,
                status_label=_STATUS_LABELS[status],
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

    categories = [
        CategoryTotal(
            slug=r["category_slug"] or "uncategorised",
            name=r["category_name"] or "Uncategorised",
            total_paise=int(r["amount_paise"]),
            trend_pct=None,
        )
        for r in (
            await session.execute(
                text(
                    "SELECT category_slug, category_name, SUM(amount_paise) AS amount_paise "
                    "FROM v_category_spend WHERE month = ANY(:keys) "
                    "GROUP BY category_slug, category_name ORDER BY 3 DESC"
                ),
                {"keys": keys},
            )
        )
        .mappings()
        .all()
    ]

    # A locked document usually has NO account: the account is not known until
    # the PDF opens. Keying the banner on accounts alone means someone with
    # twenty password-protected statements is told we found nothing and given
    # nothing to click, which is exactly when the banner matters most.
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
            average = spent // max(len(monthly_spend), 1)
            delta = top.total_paise - average
            caption = (
                f"{_rupees(top.total_paise)} — {_rupees(delta)} above your average"
                if delta > 0
                else f"{_rupees(top.total_paise)}"
            )
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
        biggest_month=biggest,
        categories=categories,
        worth_a_look=worth_a_look,
    )
