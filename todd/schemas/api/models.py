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
    EmailConnectionStatus,
    InsightStatus,
    LockMode,
    Plan,
)

AnalysisWindow = Literal[1, 3, 6, 12]
CheckState = Literal["ok", "down", "skipped"]

__all__ = [
    "AnalysisWindow",
    "CheckState",
    "GmailConnectResponse",
    "GmailStatusResponse",
    "GoalsLockedResponse",
    "HealthChecks",
    "HealthResponse",
    "InsightOut",
    "ProfileResponse",
    "ReportResponse",
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


class ReportResponse(ApiModel):
    display_name: str | None
    has_data: bool
    window: ReportWindow
    available_windows: list[AnalysisWindow]
    locked_accounts: list[LockedAccount]
    totals: ReportTotals
    monthly_spend: list[MonthlySpend]
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
