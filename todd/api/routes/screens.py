"""GET /v1/profile and GET /v1/report — whole screens, assembled server-side.

Both read the `security_invoker` views, never the tables directly, so an
unreconciled statement's rows cannot reach a report (§5, §7.3). Every display
string is written here.
"""

from __future__ import annotations

import datetime as dt
import re
import uuid
from dataclasses import dataclass
from typing import Literal

from fastapi import APIRouter, Query
from sqlalchemy import RowMapping, text
from sqlalchemy.ext.asyncio import AsyncSession

from todd.api.deps import SessionDep, SettingsDep, UserIdDep
from todd.api.errors import ApiError
from todd.db.seed.data import (
    REFUNDS_NAME,
    REFUNDS_SLUG,
    UNCATEGORIZED_NAME,
    UNCATEGORIZED_SLUG,
    category_icon,
)
from todd.log import get_logger
from todd.parsers.dates import (
    MONTH_ABBR,
    full_date,
    gap_label,
    last_complete_months,
    month_bounds,
    month_name,
    months_between,
    preceding_months,
    range_label,
    today_ist,
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
    TransactionDetailResponse,
    TransactionOut,
    TransactionStatementSource,
    UpdateTransactionRequest,
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

logger = get_logger(__name__)
router = APIRouter(prefix="/v1", tags=["screens"])

ALLOWED_WINDOWS: list[AnalysisWindow] = [1, 3, 6, 12]

#: How many months the chart offers to choose from, whatever the window is.
STRIP_MONTHS = 12

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


def short_institution(value: str | None, name: str | None = None) -> str:
    """What an account row calls the bank: "HDFC", not "HDFC Bank".

    The row is the bank's name, the account type and the last four digits on
    one line of a phone. "ICICI Bank Credit Card ··1008" wraps onto two and
    pushes the action button out of reach; "ICICI Credit Card ··1008" does
    not, and says the same thing.

    `name` is what the statement's IFSC resolved to, and it wins: the enum
    has six members, and for every other bank it can only say `unknown`.

    With neither, the empty string — not "Unknown bank". The row then reads
    "Savings ··1421", which is everything we actually know. Naming the bank
    "Unknown" states something about the user's bank; saying nothing states
    something about our parser, and only one of those is true.
    """
    if name:
        return name
    try:
        institution = Institution(value or "unknown")
    except ValueError:
        institution = Institution.UNKNOWN
    return "" if institution is Institution.UNKNOWN else _INSTITUTION_SHORT[institution]


def _short_label(institution: str | None, account_type: str, name: str | None = None) -> str:
    bank = short_institution(institution, name)
    try:
        kind = _TYPE_SHORT[AccountType(account_type)]
    except ValueError:
        kind = "account"
    # Joined on the non-empty parts: an unnamed bank must not leave a
    # doubled space, or a leading one, in the middle of a sentence.
    return " ".join(part for part in (bank, kind) if part)


def _locked_label(institution: str | None, pending: int) -> str:
    """Label for statements locked before we know whose account they are.

    Without an institution there is nothing truthful to name, so it says what
    the user can act on instead of inventing a bank. This is the dialog's
    heading only — the banner's sentence is `_locked_message`, because "locked"
    is a fine word for a heading and a useless one in "Your ___ statements
    need a password".
    """
    if institution and institution != Institution.UNKNOWN.value:
        return _INSTITUTION_SHORT[Institution(institution)]
    return "locked" if pending == 1 else f"{pending} locked"


def _added_when(added_on: dt.date | None, today: dt.date) -> str:
    """ "today", "yesterday", or "on 28 Oct"."""
    if added_on is None:
        return "you added"
    if added_on == today:
        return "you added today"
    if added_on == today - dt.timedelta(days=1):
        return "you added yesterday"
    return f"you added on {added_on.day} {MONTH_ABBR[added_on.month - 1]}"


def _locked_message(
    institution: str | None,
    account_type: str | None,
    pending: int,
    added_on: dt.date | None,
    today: dt.date,
    institution_name: str | None = None,
) -> str:
    """The banner's sentence.

    A locked PDF is a sealed envelope: the bank, the account and the period
    are all inside it, so for an upload there is nothing to name. What the
    user does remember is putting the files there, which is why the sentence
    leans on when they were added rather than on anything we claim to have
    read out of them.

    Once a document belongs to an account we have opened one of its
    statements before, so the bank can be named and is the better handle.
    """
    if account_type:
        label = _short_label(institution, account_type, institution_name)
        return f"Your {label} statements need a password"
    bank = short_institution(institution, institution_name)
    if bank:
        return f"Your {bank} statements need a password"
    noun, verb = ("statement", "is") if pending == 1 else ("statements", "are")
    return f"{pending} {noun} {_added_when(added_on, today)} {verb} password-protected"


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
                       a.institution_name,
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
                institution=short_institution(row["institution"], row["institution_name"]),
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


MONTH_KEY = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
DATE_KEY = re.compile(r"^\d{4}-\d{2}-\d{2}$")

#: Longest range the picker will answer. Far past the statements anyone
#: holds, and short of a request that would draw a hundred bars.
MAX_RANGE_DAYS = 366 * 5


@dataclass(frozen=True, slots=True)
class Window:
    """What a request is about: days, with the months they fall in.

    Every figure is counted over `start`–`end`. The months come along because
    the chart, the coverage grid and the labels are monthly — and for a preset
    or a single month the two say exactly the same thing, the dates being that
    month's own ends. For a range the user drew they do not, which is the
    point of having both.
    """

    months: list[str]
    start: dt.date
    end: dt.date
    #: True when the user picked the days. An end can then fall mid-month, so
    #: a monthly bar has to be clipped to it rather than taken from the view.
    exact: bool = False

    @property
    def label(self) -> str:
        return range_label(self.start, self.end) if self.exact else window_label(self.months)

    @property
    def day_count(self) -> int:
        return (self.end - self.start).days + 1

    def preceding(self) -> Window:
        """The window of equal length immediately before this one.

        What every trend is measured against. Months for a month window —
        Apr–Sep is judged against Oct–Mar, never "the same months last year" —
        and the same number of days for a range, ending the day before it
        starts.
        """
        if not self.exact:
            return from_months(preceding_months(self.months))
        end = self.start - dt.timedelta(days=1)
        start = end - dt.timedelta(days=self.day_count - 1)
        return Window(months=months_between(start, end), start=start, end=end, exact=True)


def from_months(keys: list[str]) -> Window:
    """A whole-month window: the first day of the first to the last of the last."""
    if not keys:
        # An empty month list means an empty ledger. A window of today over
        # today counts nothing, which is the honest answer.
        today = today_ist()
        return Window(months=[], start=today, end=today)
    return Window(months=keys, start=month_bounds(keys[0])[0], end=month_bounds(keys[-1])[1])


def _exact_window(start: str, end: str) -> Window:
    """A range the user drew, validated.

    Both ends or neither: a half-given range would silently become something
    else — a preset, or the saved window — and the report would answer a
    question nobody asked.
    """
    if not (DATE_KEY.match(start) and DATE_KEY.match(end)):
        raise ApiError(400, "bad_request", "A date looks like 2026-07-30.")
    try:
        first, last = dt.date.fromisoformat(start), dt.date.fromisoformat(end)
    except ValueError:
        raise ApiError(400, "bad_request", "That is not a date.") from None
    if last < first:
        raise ApiError(400, "bad_request", "The first date has to come before the second.")
    if (last - first).days + 1 > MAX_RANGE_DAYS:
        raise ApiError(400, "bad_request", "A range can cover at most five years.")
    return Window(months=months_between(first, last), start=first, end=last, exact=True)


async def resolve_window(
    session: SessionDep,
    *,
    months: str | None,
    month: str | None,
    start: str | None = None,
    end: str | None = None,
) -> Window:
    """Which days this request is about.

    Four ways to ask, in order of precedence:

    * `from=2026-07-30&to=2026-09-30` — exactly those days. Neither a preset
      nor a month can express "the end of July to the end of September", and
      that is the shape a question about one trip, one job or one tenancy
      takes.
    * `month=2026-04` — that month alone. The month strip on Home taps
      straight through to this, which is the only way to see one month: the
      presets can only ever mean "the last N", never "that one".
    * `months=all` — every month from the first statement to last month.
    * `months=3|6|12` — the presets, or the window saved on the profile.
    """
    if start or end:
        if not (start and end):
            raise ApiError(400, "bad_request", "A date range needs both a start and an end.")
        return _exact_window(start, end)

    if month:
        if not MONTH_KEY.match(month):
            raise ApiError(400, "bad_request", "A month looks like 2026-04.")
        return from_months([month])

    if (months or "").strip().lower() == "all":
        earliest = (
            await session.execute(
                text("SELECT to_char(min(txn_date), 'YYYY-MM') FROM v_publishable_transactions")
            )
        ).scalar_one_or_none()
        latest = last_complete_months(1)[0]
        if not earliest:
            return from_months(last_complete_months(await _window_for(session, None)))
        # Cheaper than a date series, and the two ends are all we have anyway.
        span = (int(latest[:4]) - int(earliest[:4])) * 12 + int(latest[5:7]) - int(earliest[5:7])
        return from_months(last_complete_months(max(span + 1, 1)))

    requested: int | None = None
    if months:
        try:
            requested = int(months)
        except ValueError:
            raise ApiError(400, "bad_request", "That is not a window.") from None
    return from_months(last_complete_months(await _window_for(session, requested)))


@router.get("/report", response_model=ReportResponse, summary="Home — the whole report")
async def get_report(
    session: SessionDep,
    user_id: UserIdDep,
    months: str | None = Query(
        default=None, description="1, 3, 6, 12 or 'all'. Omit for the saved one."
    ),
    month: str | None = Query(default=None, description="One month, as 2026-04. Beats `months`."),
    date_from: str | None = Query(
        default=None,
        alias="from",
        description="First day, as 2026-07-30. With `to`, beats everything else.",
    ),
    date_to: str | None = Query(default=None, alias="to", description="Last day, as 2026-09-30."),
) -> ReportResponse:
    window = await resolve_window(session, months=months, month=month, start=date_from, end=date_to)
    keys = window.months
    month_count = len(keys)
    bounds = {"start": window.start, "end": window.end}

    # The chart is a picker, so it shows more than the window: you cannot
    # choose April from a strip that only has the six months you are already
    # looking at, and a single month on its own would be a dead end. The last
    # twelve, plus whatever the window reaches beyond them.
    strip = sorted(set(last_complete_months(STRIP_MONTHS)) | set(keys))
    in_window = set(keys)

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
                {"keys": strip},
            )
        )
        .mappings()
        .all()
    )

    by_month: dict[str, dict[str, int]] = {
        r["month"]: {"spent": int(r["spent"]), "received": int(r["received"])} for r in monthly
    }
    # A month with no statement is not a month where nothing was spent, so the
    # chart is told which is which rather than drawing both as zero.
    coverage = (
        (
            await session.execute(
                text(
                    "SELECT month, count(DISTINCT account_id) AS accounts "
                    "FROM v_statement_coverage WHERE month = ANY(:keys) GROUP BY month"
                ),
                {"keys": strip},
            )
        )
        .mappings()
        .all()
    )
    if window.exact:
        # The ends fall mid-month, so the months at either end of the window
        # are not the months `v_monthly_spend` holds: counting the whole of
        # July into a range that starts on the 30th would put twenty-nine days
        # nobody asked for under the bar, and into the total above it. Only
        # the window's own months are replaced — the rest of the strip is
        # still the whole month, because that is what those bars mean.
        for key in keys:
            by_month[key] = {"spent": 0, "received": 0}
        for row in (
            (
                await session.execute(
                    text(
                        "SELECT to_char(txn_date, 'YYYY-MM') AS month, "
                        "  COALESCE(SUM(amount_paise) FILTER (WHERE direction = 'debit'), 0) "
                        "    AS spent, "
                        "  COALESCE(SUM(amount_paise) FILTER (WHERE direction = 'credit'), 0) "
                        "    AS received "
                        "FROM v_publishable_transactions "
                        "WHERE NOT is_transfer AND txn_date BETWEEN :start AND :end "
                        "GROUP BY 1"
                    ),
                    bounds,
                )
            )
            .mappings()
            .all()
        ):
            by_month[row["month"]] = {
                "spent": int(row["spent"]),
                "received": int(row["received"]),
            }

    accounts_by_month = {row["month"]: int(row["accounts"]) for row in coverage}
    # Against every account that has ever produced a statement: a month only
    # one of three banks covers is real, but it is not the whole picture.
    account_total = (
        await session.execute(text("SELECT count(DISTINCT account_id) FROM v_statement_coverage"))
    ).scalar_one_or_none() or 0

    monthly_spend = [
        MonthlySpend(
            month=k,
            total_paise=by_month.get(k, {}).get("spent", 0),
            covered=k in accounts_by_month,
            complete=accounts_by_month.get(k, 0) >= account_total > 0,
            in_window=k in in_window,
        )
        for k in strip
    ]
    # Only what the figures above are actually made of.
    incomplete = sum(1 for m in monthly_spend if m.in_window and m.covered and not m.complete)
    spent = sum(m.total_paise for m in monthly_spend if m.in_window)
    money_in = sum(by_month.get(k, {}).get("received", 0) for k in keys)

    # Both windows in one pass: the one on screen, and the equally long one
    # before it that every trend is measured against. A category with no
    # history gets no trend at all — "up 100%" from nothing is not a fact
    # about spending, it is a fact about when the statements start.
    previous = window.preceding()
    # A trend is only a fact about spending if we hold the months it compares
    # against. Against a window with statements for one month out of six it
    # reads "Travel & fuel up 3913%", which says nothing about travel and
    # everything about where the statements stop.
    covered_before = (
        await session.execute(
            text("SELECT count(DISTINCT month) FROM v_statement_coverage WHERE month = ANY(:keys)"),
            {"keys": previous.months},
        )
    ).scalar_one_or_none() or 0
    comparable = covered_before == len(previous.months)

    categories = [
        CategoryTotal(
            slug=r["category_slug"] or UNCATEGORIZED_SLUG,
            name=r["category_name"] or UNCATEGORIZED_NAME,
            icon=category_icon(r["category_slug"] or UNCATEGORIZED_SLUG),
            total_paise=int(r["amount_paise"]),
            trend_pct=(
                _trend_pct(int(r["amount_paise"]), int(r["previous_paise"] or 0))
                if comparable
                else None
            ),
            txn_count=int(r["txn_count"] or 0),
        )
        for r in (
            await session.execute(
                # `v_category_spend` by another name: the same predicates, the
                # same grouping, bounded by dates instead of month keys. The
                # view cannot answer a range that starts on the 30th, and two
                # queries — one per shape — is one of them drifting from the
                # other. Over whole months the two agree exactly, because the
                # bounds are then the months' own ends.
                text(
                    "SELECT c.slug AS category_slug, c.name AS category_name, "
                    "       COALESCE(SUM(t.amount_paise) FILTER (WHERE :start <= t.txn_date "
                    "         AND t.txn_date <= :end), 0) AS amount_paise, "
                    "       count(*) FILTER (WHERE :start <= t.txn_date "
                    "         AND t.txn_date <= :end) AS txn_count, "
                    "       COALESCE(SUM(t.amount_paise) FILTER (WHERE :pstart <= t.txn_date "
                    "         AND t.txn_date <= :pend), 0) AS previous_paise "
                    "FROM v_publishable_transactions t "
                    "LEFT JOIN categories c ON c.id = t.category_id "
                    "WHERE NOT t.is_transfer AND t.direction = 'debit' "
                    "  AND (t.txn_date BETWEEN :start AND :end "
                    "       OR t.txn_date BETWEEN :pstart AND :pend) "
                    "GROUP BY c.slug, c.name "
                    "HAVING COALESCE(SUM(t.amount_paise) FILTER (WHERE :start <= t.txn_date "
                    "         AND t.txn_date <= :end), 0) > 0 "
                    "ORDER BY 3 DESC"
                ),
                {**bounds, "pstart": previous.start, "pend": previous.end},
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
                    "WHERE t.txn_date BETWEEN :start AND :end "
                    "  AND t.direction = 'credit' AND NOT t.is_transfer "
                    "GROUP BY c.slug, c.name, c.kind ORDER BY 4 DESC"
                ),
                bounds,
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
                icon=category_icon(row["slug"] or "income_other"),
                total_paise=int(row["total"]),
                trend_pct=None,
            )
        )
    if refunds:
        income_sources.append(
            IncomeSource(
                slug=REFUNDS_SLUG,
                name=REFUNDS_NAME,
                icon=category_icon(REFUNDS_SLUG),
                total_paise=refunds,
                trend_pct=None,
            )
        )
    income_sources.sort(key=lambda source: source.total_paise, reverse=True)

    locked = [
        LockedAccount(
            id=str(r["id"]),
            label=(
                _short_label(r["institution"], r["type"], r["institution_name"])
                if r["type"]
                else _locked_label(r["institution"], int(r["pending"]))
            ),
            message=_locked_message(
                r["institution"],
                r["type"],
                int(r["pending"]),
                r["added_on"],
                today_ist(),
                r["institution_name"],
            ),
        )
        for r in (
            await session.execute(
                text(
                    """
                    SELECT a.id,
                           a.institution::text AS institution,
                           a.type::text        AS type,
                           a.institution_name,
                           1                   AS pending,
                           NULL::date          AS added_on
                    FROM accounts a
                    WHERE EXISTS (SELECT 1 FROM documents d
                                  WHERE d.account_id = a.id AND d.status = 'locked')
                    UNION ALL
                    -- One row per day's upload, not per bank. A sealed file
                    -- rarely names its bank, so grouping on the institution
                    -- put every unreadable statement in one "unknown" pile;
                    -- grouping on the day the user added them is the thing
                    -- the sentence is about, and keeps every count true.
                    SELECT DISTINCT ON (added_on)
                           d.id,
                           d.institution::text AS institution,
                           NULL                AS type,
                           NULL                AS institution_name,
                           count(*) OVER (PARTITION BY added_on) AS pending,
                           added_on
                    FROM (
                        SELECT *,
                               (created_at AT TIME ZONE 'Asia/Kolkata')::date AS added_on
                        FROM documents
                    ) d
                    WHERE d.status = 'locked' AND d.account_id IS NULL
                    ORDER BY added_on DESC
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
        top = max((m for m in monthly_spend if m.in_window), key=lambda m: m.total_paise)
        if top.total_paise > 0:
            # The chart already draws how much bigger it was; the caption names
            # *which* month, so the highlight never rests on colour alone.
            caption = f"{month_name(top.month)} was your biggest month."
            biggest = BiggestMonth(month=top.month, caption=caption)

    # Saved is only answerable when something came in. With no credits in the
    # window — a card statement on its own, a month before the salary lands —
    # every rupee spent would read as overspending, which says nothing about
    # the money and everything about which statements we hold.
    saved: int | None = None
    saved_pct: float | None = None
    if money_in > 0:
        saved = money_in - spent
        saved_pct = round(saved / money_in * 100, 1)

    return ReportResponse(
        display_name=user["display_name"] if user else None,
        has_data=bool(monthly or categories),
        window=ReportWindow(
            months=keys,
            label=window.label,
            month_count=month_count,
            incomplete_months=incomplete,
            start=window.start.isoformat(),
            end=window.end.isoformat(),
            day_count=window.day_count,
            exact=window.exact,
        ),
        available_windows=ALLOWED_WINDOWS,
        locked_accounts=locked,
        totals=ReportTotals(
            spent_paise=spent,
            money_in_paise=money_in,
            saved_paise=saved,
            saved_pct_of_income=saved_pct,
        ),
        monthly_spend=monthly_spend,
        income_sources=income_sources,
        biggest_month=biggest,
        categories=categories,
        worth_a_look=worth_a_look,
    )


# ---------------------------------------------- category drill-down (new)


#: Every column `_transaction_out` reads. One list, so the drill-down and the
#: detail screen cannot select different things and then disagree about the
#: same row.
TRANSACTION_COLUMNS = """
    t.id, t.txn_date, t.amount_paise, t.direction::text AS direction,
    t.raw_description, t.counterparty_name, t.counterparty_vpa, t.ref_no,
    t.category_source, t.category_confidence, t.is_transfer, t.note,
    m.normalized_name AS merchant_name,
    c.slug AS category_slug, c.name AS category_name,
    a.institution::text AS institution, a.type::text AS account_type,
    a.institution_name,
    a.masked_number
"""


def _future_rule_label(row: RowMapping) -> str | None:
    """What "do this for future … payments too" would actually match on.

    It has to name the same thing `file_transaction` writes a rule for — the
    UPI address, or the payee pulled out of the narration. Naming the rail
    instead ("future UPI payments") promises to file half the ledger under
    one category, which is not what the tick does.
    """
    return (
        row["counterparty_vpa"]
        or row["counterparty_name"]
        or row["merchant_name"]
        or extract_payee(row["raw_description"])
    )


def _transaction_out(row: RowMapping) -> TransactionOut:
    """One ledger row, as every screen that lists transactions shows it."""
    return TransactionOut(
        id=str(row["id"]),
        txn_date=row["txn_date"].isoformat(),
        amount_paise=int(row["amount_paise"]),
        direction=Direction(row["direction"]),
        raw_description=row["raw_description"],
        title=row_title(
            description=row["raw_description"],
            merchant_name=row["merchant_name"],
            counterparty_name=row["counterparty_name"],
            counterparty_vpa=row["counterparty_vpa"],
        ),
        rail=extract_rail(row["raw_description"]),
        account_masked=row["masked_number"],
        merchant_name=row["merchant_name"],
        counterparty_name=row["counterparty_name"],
        counterparty_vpa=row["counterparty_vpa"],
        category_slug=row["category_slug"] or UNCATEGORIZED_SLUG,
        category_name=row["category_name"] or UNCATEGORIZED_NAME,
        category_source=row["category_source"],
        category_confidence=(
            float(row["category_confidence"]) if row["category_confidence"] is not None else None
        ),
        is_transfer=row["is_transfer"],
        account_label=_short_label(
            row["institution"], row["account_type"] or "", row["institution_name"]
        ),
        future_rule_label=_future_rule_label(row),
    )


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
    months: str | None = Query(default=None, description="1, 3, 6, 12 or 'all'."),
    month: str | None = Query(default=None, description="One month, as 2026-04. Beats `months`."),
    date_from: str | None = Query(
        default=None, alias="from", description="First day, as 2026-07-30. Beats everything else."
    ),
    date_to: str | None = Query(default=None, alias="to", description="Last day, as 2026-09-30."),
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
    # The same selection the bar was drawn for: a drill-down that quietly
    # falls back to the saved window shows a total the user never clicked.
    chosen = await resolve_window(session, months=months, month=month, start=date_from, end=date_to)
    window = ReportWindow(
        months=chosen.months,
        label=chosen.label,
        month_count=len(chosen.months),
        start=chosen.start.isoformat(),
        end=chosen.end.isoformat(),
        day_count=chosen.day_count,
        exact=chosen.exact,
    )

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
        "start": chosen.start,
        "end": chosen.end,
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
        WHERE t.txn_date BETWEEN :start AND :end
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
                    f"SELECT {TRANSACTION_COLUMNS} {base} "
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
        icon=category_icon(slug),
        window=window,
        total_paise=int(summary["total"]),
        count=int(summary["n"]),
        transactions=[_transaction_out(r) for r in rows],
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
            CategoryOption(
                slug=r["slug"],
                name=r["name"],
                kind=CategoryKind(r["kind"]),
                icon=category_icon(r["slug"]),
            )
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


# ------------------------------------------------ one transaction, in full


#: What the account type is called when it sits beside the bank's name on the
#: Details table: "ICICI Card ··9012".
_TYPE_DETAIL: dict[AccountType, str] = {
    AccountType.SAVINGS: "Bank",
    AccountType.CREDIT_CARD: "Card",
    AccountType.LOAN: "Loan",
}

#: Why the row is filed where it is, per rung of the ladder (§9). Shown under
#: the category name: a label with no reason behind it is one the user has no
#: way to argue with, and arguing with it is the whole point of the screen.
_CATEGORY_NOTES: dict[str, str] = {
    "user_rule": "You filed this one",
    "own_transfer_pair": "Matched to a move between your own accounts",
    "bank_charge": "The bank's own charge",
    "structural": "Read from the way the bank described it",
    "mandate": "A standing mandate — an EMI, a SIP or a premium",
    "merchant_dict": "We recognised the merchant",
    "vpa_person": "A person's UPI address, not a shop",
    "vpa_business": "A business UPI handle, but not what it sells",
    "payee_trigram": "Same payee as one you filed before",
    "payee_classify": "Our best guess",
    "llm": "Our best guess",
    "llm_unknown": "We weren't sure about this one",
    "unmatched": "We weren't sure about this one",
}

_UNSURE = "We weren't sure about this one"


def _account_line(
    institution: str | None,
    account_type: str | None,
    masked: str | None,
    institution_name: str | None = None,
) -> str:
    """ "ICICI Card ··9012" — the Details row for the account.

    Last 4 only, and the dots are part of the string rather than left to the
    client to prepend: a client that forgets them prints what looks like a
    whole account number (§12).
    """
    name = short_institution(institution, institution_name)
    try:
        kind = _TYPE_DETAIL[AccountType(account_type or "")]
    except ValueError:
        kind = "Account"
    head = " ".join(part for part in (name, kind) if part)
    return f"{head} ··{masked}" if masked else head


def _method_label(account_type: str | None, rail: str | None) -> str:
    """How the money moved: "Card · POS", "UPI", "Card".

    The instrument is only named for a card, because that is the case where
    the rail alone misleads — a POS on a credit card and a POS on a debit
    card are the same rail and different money.
    """
    if account_type == AccountType.CREDIT_CARD.value:
        return f"Card · {rail}" if rail else "Card"
    return rail or "Payment"


def _category_note(source: str | None, merchant_name: str | None) -> str:
    """One line saying which rung decided this, in words rather than a slug."""
    if source == "merchant_dict" and merchant_name:
        return f"We recognised {merchant_name}"
    return _CATEGORY_NOTES.get(source or "", _UNSURE)


async def _load_detail(session: AsyncSession, txn_id: uuid.UUID) -> TransactionDetailResponse:
    """Assemble the detail screen for one row, or 404.

    Reads `v_publishable_transactions` like every other read, so a row from a
    statement that failed the reconciliation gate is not reachable here
    either — it is not in the ledger the user is looking at (§5, §7.3).
    """
    row = (
        (
            await session.execute(
                text(
                    f"SELECT {TRANSACTION_COLUMNS}, "
                    "       d.id AS document_id, d.filename, "
                    "       s.id AS statement_id, s.period_start, s.period_end "
                    "FROM v_publishable_transactions t "
                    "LEFT JOIN categories c ON c.id = t.category_id "
                    "LEFT JOIN merchants  m ON m.id = t.merchant_id "
                    "LEFT JOIN accounts   a ON a.id = t.account_id "
                    "LEFT JOIN statements s ON s.id = t.statement_id "
                    "LEFT JOIN documents  d ON d.id = s.document_id "
                    "WHERE t.id = :id"
                ),
                {"id": txn_id},
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise ApiError(404, "not_found", "No such transaction.")

    transaction = _transaction_out(row)

    # A statement the user can be pointed back to. An alert row has none, and
    # the card is left off rather than filled with "unknown".
    statement: TransactionStatementSource | None = None
    if row["filename"] and row["period_start"] and row["period_end"]:
        statement = TransactionStatementSource(
            document_id=str(row["document_id"]) if row["document_id"] else None,
            statement_id=str(row["statement_id"]) if row["statement_id"] else None,
            filename=row["filename"],
            period_label=f"Statement for {range_label(row['period_start'], row['period_end'])}",
        )

    return TransactionDetailResponse(
        transaction=transaction,
        date_label=full_date(row["txn_date"]),
        account_line=_account_line(
            row["institution"],
            row["account_type"],
            row["masked_number"],
            row["institution_name"],
        ),
        method_label=_method_label(row["account_type"], transaction.rail),
        ref_no=row["ref_no"],
        type_label="Credit" if transaction.direction is Direction.CREDIT else "Debit",
        category_icon=category_icon(transaction.category_slug),
        category_note=_category_note(row["category_source"], row["merchant_name"]),
        note=row["note"],
        statement=statement,
    )


def _transaction_id(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise ApiError(400, "bad_request", "That is not a transaction id.") from exc


@router.get(
    "/transactions/{transaction_id}",
    response_model=TransactionDetailResponse,
    summary="One transaction, in full",
)
async def get_transaction(
    transaction_id: str,
    session: SessionDep,
    user_id: UserIdDep,
) -> TransactionDetailResponse:
    """The detail screen: the row, where it came from, and why it is filed there."""
    return await _load_detail(session, _transaction_id(transaction_id))


@router.patch(
    "/transactions/{transaction_id}",
    response_model=TransactionDetailResponse,
    summary="Add a note, or mark a transaction as not a spend",
)
async def update_transaction(
    transaction_id: str,
    body: UpdateTransactionRequest,
    session: SessionDep,
    user_id: UserIdDep,
) -> TransactionDetailResponse:
    """The two buttons at the foot of the detail screen.

    "Not a spend" sets `isTransfer`, which is what the report already excludes
    — paying your own card from savings is not spending. It is deliberately
    not a delete: the row stays in the ledger, because a statement that
    reconciles must keep reconciling after the user touches a screen.

    A field left out of the body is left alone. `note: null` clears it.
    """
    txn_id = _transaction_id(transaction_id)
    sent = body.model_fields_set

    updates: dict[str, object] = {}
    if "note" in sent:
        note = (body.note or "").strip()
        updates["note"] = note or None
    if "is_transfer" in sent and body.is_transfer is not None:
        updates["is_transfer"] = body.is_transfer

    if updates:
        assignments = ", ".join(f"{column} = :{column}" for column in updates)
        result = await session.execute(
            text(f"UPDATE transactions SET {assignments}, updated_at = now() WHERE id = :id"),
            {**updates, "id": txn_id},
        )
        # No commit: `scoped_session` holds the transaction open and commits
        # on a clean return. Committing here closes it under the read below.
        if not getattr(result, "rowcount", 0):
            raise ApiError(404, "not_found", "No such transaction.")
        logger.info("transaction_updated", fields=sorted(updates))

    return await _load_detail(session, txn_id)
