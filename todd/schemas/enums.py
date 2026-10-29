"""Every enum declared exactly once; the Postgres types are built from these.

A new institution must be a one-line change here, not three. `db/models.py`
builds `sa.Enum(..., name=...)` from these classes so the Python and Postgres
sides cannot drift.
"""

from enum import IntEnum, StrEnum


class Plan(StrEnum):
    FREE = "free"
    PRO = "pro"


class AnalysisWindowMonths(IntEnum):
    """Not a Postgres enum — an integer column with a check constraint."""

    ONE = 1
    THREE = 3
    SIX = 6
    TWELVE = 12


class LockMode(StrEnum):
    UPGRADE = "upgrade"
    WAITLIST = "waitlist"


class Institution(StrEnum):
    HDFC = "hdfc"
    ICICI = "icici"
    SBI = "sbi"
    AXIS = "axis"
    KOTAK = "kotak"
    BOB = "bob"
    UNKNOWN = "unknown"


INSTITUTION_LABELS: dict[Institution, str] = {
    Institution.HDFC: "HDFC Bank",
    Institution.ICICI: "ICICI Bank",
    Institution.SBI: "State Bank of India",
    Institution.AXIS: "Axis Bank",
    Institution.KOTAK: "Kotak Mahindra Bank",
    Institution.BOB: "Bank of Baroda",
    Institution.UNKNOWN: "Unknown bank",
}


class ConsentPurpose(StrEnum):
    STATEMENT_ANALYSIS = "statement_analysis"
    PASSWORD_GUESSING = "password_guessing"
    PUSH_NOTIFICATIONS = "push_notifications"


class EmailProvider(StrEnum):
    GMAIL = "gmail"


class EmailConnectionStatus(StrEnum):
    ACTIVE = "active"
    REVOKED = "revoked"
    ERROR = "error"


class RawMessageKind(StrEnum):
    STATEMENT = "statement"
    TXN_ALERT = "txn_alert"
    IGNORE = "ignore"


class DocumentKind(StrEnum):
    STATEMENT_PDF = "statement_pdf"


class DocumentStatus(StrEnum):
    QUEUED = "queued"
    LOCKED = "locked"
    PARSING = "parsing"
    NEEDS_REVIEW = "needs_review"
    DONE = "done"
    FAILED = "failed"


class AccountType(StrEnum):
    SAVINGS = "savings"
    CREDIT_CARD = "credit_card"
    LOAN = "loan"


class AccountSource(StrEnum):
    DETECTED = "detected"
    USER_ADDED = "user_added"


class AccountStatus(StrEnum):
    """Drives the row's action on Profile: nothing, Unlock, or Upload."""

    UP_TO_DATE = "up_to_date"
    NEEDS_PASSWORD = "needs_password"
    MISSING_MONTHS = "missing_months"


class CoverageStatus(StrEnum):
    RECEIVED = "received"
    LOCKED = "locked"
    MISSING = "missing"
    NEEDS_REVIEW = "needs_review"


class TransactionSource(StrEnum):
    STATEMENT = "statement"
    ALERT = "alert"
    MANUAL = "manual"


class Direction(StrEnum):
    DEBIT = "debit"
    CREDIT = "credit"


class CategoryKind(StrEnum):
    SPEND = "spend"
    INCOME = "income"
    TRANSFER = "transfer"


class RuleMatchKind(StrEnum):
    MERCHANT = "merchant"
    VPA = "vpa"
    TEXT = "text"


class InsightType(StrEnum):
    CATEGORY_TREND = "category_trend"
    DUPLICATE_SUBSCRIPTION = "duplicate_subscription"
    FEES = "fees"
    UPI_TO_PEOPLE = "upi_to_people"
    UPCOMING_RENEWAL = "upcoming_renewal"
    BIGGEST_MONTH = "biggest_month"
    COVERAGE_GAP = "coverage_gap"


class InsightStatus(StrEnum):
    NEW = "new"
    SEEN = "seen"
    DISMISSED = "dismissed"


class SecretKind(StrEnum):
    PDF_PASSWORD = "pdf_password"


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class BackfillState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


class ParseEngine(StrEnum):
    BANK_PARSER = "bank_parser"
    OCR = "ocr"
    LLM = "llm"
    TABLE_EXTRACT = "table_extract"


class ParseStrategy(StrEnum):
    """Application-level only; never reaches Postgres.

    Geometry first: it is free, and on real statements it verified roughly
    three in four of them to the paise. The model is the fallback.
    """

    TABLE_EXTRACT = "table_extract"
    LLM = "llm"
    BANK_PARSER = "bank_parser"


DEFAULT_STRATEGY_ORDER: list[ParseStrategy] = [
    ParseStrategy.TABLE_EXTRACT,
    ParseStrategy.LLM,
]
