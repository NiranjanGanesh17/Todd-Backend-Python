"""Request and response models for every route.

These mirror `Todd-Frontend/contracts/*.ts` exactly. The web app parses every
response through its Zod schema, so an extra `null` or a snake_case key is a
hard client failure, not a cosmetic difference.

Display strings — `statusLabel`, `window.label`, `biggestMonth.caption`,
`institution` — are written here, server-side, so two clients cannot phrase the
same number differently (§6).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from todd.schemas.api.base import ApiModel
from todd.schemas.enums import (
    AccountStatus,
    AccountType,
    CategoryKind,
    Direction,
    EmailConnectionStatus,
    InsightStatus,
    LockMode,
    Plan,
)

AnalysisWindow = Literal[1, 3, 6, 12]
CheckState = Literal["ok", "down", "skipped"]

__all__ = [
    "AnalysisWindow",
    "CategoriesResponse",
    "CategoryOption",
    "CategoryTransactionsResponse",
    "CheckState",
    "FileTransactionRequest",
    "FileTransactionResponse",
    "GmailConnectResponse",
    "GmailStatusResponse",
    "GoalsLockedResponse",
    "HealthChecks",
    "HealthResponse",
    "IncomeSource",
    "InsightOut",
    "ProfileResponse",
    "ReportResponse",
    "TransactionOut",
    "UnlockRequest",
    "UnlockResponse",
    "UploadResponse",
    "WaitlistResponse",
]


# ------------------------------------------------------------------- health


class HealthChecks(ApiModel):
    database: CheckState
    redis: CheckState


class HealthResponse(ApiModel):
    status: Literal["ok"] = "ok"
    service: str
    version: str
    uptime_seconds: float
    checks: HealthChecks


# ------------------------------------------------------------------ shared


class InsightOut(ApiModel):
    id: str
    type: str
    title: str
    body: str
    payload: dict[str, Any] = Field(default_factory=dict)
    #: The contract types this as a plain string, so a period-less insight
    #: sends "" rather than null.
    period: str = ""
    status: InsightStatus


# ------------------------------------------------------------------ report


class ReportWindow(ApiModel):
    months: list[str]
    label: str
    month_count: AnalysisWindow


class LockedAccount(ApiModel):
    id: str
    #: Short label for the banner, e.g. "ICICI card".
    label: str


class ReportTotals(ApiModel):
    spent_paise: int
    money_in_paise: int
    saved_pct_of_income: float | None


class MonthlySpend(ApiModel):
    month: str
    total_paise: int


class BiggestMonth(ApiModel):
    month: str
    caption: str


class CategoryTotal(ApiModel):
    slug: str
    name: str
    total_paise: int
    trend_pct: float | None
    #: How many transactions make up the total. The home screen prints it only
    #: for the uncategorized bucket, where it is a count of work to do
    #: ("6 to sort") rather than a statistic.
    txn_count: int = 0


class IncomeSource(ApiModel):
    """One source of money arriving, for the "Came in" breakdown.

    Same shape as a spend category, because it is one — the credit side of
    the same ledger, filtered the other way.
    """

    slug: str
    name: str
    total_paise: int
    trend_pct: float | None


class ReportResponse(ApiModel):
    display_name: str | None
    has_data: bool
    window: ReportWindow
    available_windows: list[AnalysisWindow]
    locked_accounts: list[LockedAccount]
    totals: ReportTotals
    monthly_spend: list[MonthlySpend]
    #: Where the money in `totals.moneyInPaise` came from, largest first.
    income_sources: list[IncomeSource]
    biggest_month: BiggestMonth | None
    categories: list[CategoryTotal]
    worth_a_look: list[InsightOut]


# ----------------------------------------------------------------- profile


class ProfileAccount(ApiModel):
    id: str
    #: The display label ("ICICI Bank"), not the slug — the web app renders
    #: this string directly.
    institution: str
    type: AccountType
    #: Last 4 only. Full numbers never leave the parser.
    masked_number: str | None
    status: AccountStatus
    #: Server-written, e.g. "Up to date" or "Missing Jul – Sep".
    status_label: str


class GmailSummary(ApiModel):
    connected: bool
    email: str | None


class ProfileResponse(ApiModel):
    display_name: str | None
    email: str
    plan: Plan
    accounts: list[ProfileAccount]
    gmail: GmailSummary


# ------------------------------------------------------------------- gmail


class GmailConnection(ApiModel):
    id: str
    provider: Literal["gmail"] = "gmail"
    google_email: str | None
    status: EmailConnectionStatus
    scopes: list[str]
    last_synced_at: str | None
    watch_expires_at: str | None
    created_at: str


class GmailBackfill(ApiModel):
    state: Literal["pending", "running", "done", "failed"]
    messages_found: int
    messages_processed: int
    statements_found: int
    alerts_found: int
    started_at: str | None
    finished_at: str | None


class GmailStatusResponse(ApiModel):
    connection: GmailConnection | None
    backfill: GmailBackfill | None


class GmailConnectResponse(ApiModel):
    auth_url: str


# --------------------------------------------------------------- documents


class UploadResponse(ApiModel):
    accepted: int
    #: Files that opened straight away, versus ones waiting on a password.
    locked: int
    duplicates: int


class UnlockRequest(ApiModel):
    account_id: str
    password: str = Field(min_length=1)
    #: Stored encrypted only when the user asks us to remember it.
    remember: bool = False


class UnlockResponse(ApiModel):
    unlocked: int
    still_locked: int


# ------------------------------------------------------------------- goals


class GoalTeaser(ApiModel):
    type: str
    title: str
    body: str


class GoalsLockedResponse(ApiModel):
    locked: Literal[True] = True
    mode: LockMode
    price_inr: float | None
    teasers: list[GoalTeaser]
    on_waitlist: bool


class WaitlistResponse(ApiModel):
    on_waitlist: Literal[True] = True


# ------------------------------------------------- category drill-down (new)


class TransactionOut(ApiModel):
    """One ledger row, as a category drill-down shows it.

    `merchantName` or `counterpartyName` is what a person reads;
    `rawDescription` is kept so they can see what the bank actually printed
    when a label looks wrong.
    """

    id: str
    txn_date: str
    amount_paise: int
    direction: Direction
    raw_description: str
    #: Server-written, so two screens cannot phrase the same row differently
    #: (§6). Shape is `RAIL / who`, e.g. "UPI / ramesh.k@okaxis".
    title: str
    #: The payment rail — UPI, NEFT, POS, ACH. What a person recognises first.
    rail: str | None
    #: Last 4 of the account it moved through. Never more than 4 (§12).
    account_masked: str | None
    merchant_name: str | None
    counterparty_name: str | None
    counterparty_vpa: str | None
    category_slug: str | None
    category_name: str | None
    #: Which rung of the ladder decided this (§9) — `merchant_dict`,
    #: `vpa_person`, `unmatched`, and so on.
    category_source: str | None
    category_confidence: float | None
    is_transfer: bool
    account_label: str


class CategoryOption(ApiModel):
    """A category a transaction can be filed under."""

    slug: str
    name: str
    kind: CategoryKind


class CategoriesResponse(ApiModel):
    categories: list[CategoryOption]


class FileTransactionRequest(ApiModel):
    category_slug: str
    #: Apply to every future transaction from the same payee, not just this
    #: one. This is what makes sorting compound instead of being busywork.
    apply_to_future: bool = False


class FileTransactionResponse(ApiModel):
    id: str
    category_slug: str
    category_name: str
    #: How many rows this one decision moved, including future-proofing.
    affected: int


class CategoryTransactionsResponse(ApiModel):
    slug: str
    name: str
    window: ReportWindow
    total_paise: int
    #: Total rows matching, which may exceed those returned.
    count: int
    transactions: list[TransactionOut]
