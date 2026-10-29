"""SQLAlchemy 2.0 models, mirroring `migrations/sql/0001_baseline.sql`.

The SQL is the source of truth for the schema — it carries the RLS policies,
the five `security_invoker` views and the check constraints. These models exist
so application code has typed access to the same tables; `tests/test_schema.py`
asserts the two have not drifted.

Money is `BigInteger` holding integer paise. psycopg3 returns a Python `int`
for `bigint` with no configuration, unlike `pg` in Node.
"""

from __future__ import annotations

import datetime as dt
import uuid
from enum import Enum as PyEnum
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from todd.schemas import enums


class Base(DeclarativeBase):
    pass


def pg_enum(python_enum: type[PyEnum], name: str) -> sa.Enum:
    """Build the Postgres type from the Python enum.

    `values_callable` is essential: without it SQLAlchemy sends member *names*
    ("CREDIT_CARD") where Postgres expects member *values* ("credit_card").
    """
    return sa.Enum(
        python_enum,
        name=name,
        values_callable=lambda e: [m.value for m in e],
        create_type=False,
    )


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")
    )


def _created_at() -> Mapped[dt.datetime]:
    return mapped_column(sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)


def _user_fk() -> Mapped[uuid.UUID]:
    return mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )


# --------------------------------------------------------------------- users


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    email: Mapped[str] = mapped_column(sa.String(320), nullable=False)
    display_name: Mapped[str | None] = mapped_column(sa.String(80))
    plan: Mapped[enums.Plan] = mapped_column(
        pg_enum(enums.Plan, "plan"), server_default="free", nullable=False
    )
    pro_waitlist_at: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True))
    analysis_window_months: Mapped[int] = mapped_column(
        sa.Integer, server_default="6", nullable=False
    )
    date_of_birth: Mapped[dt.date | None] = mapped_column(sa.Date)
    deletion_requested_at: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True))
    created_at: Mapped[dt.datetime] = _created_at()
    updated_at: Mapped[dt.datetime] = _created_at()

    __table_args__ = (
        sa.CheckConstraint("analysis_window_months in (1, 3, 6, 12)", name="users_window_months"),
        sa.Index("users_email_key", sa.text("lower(email)"), unique=True),
    )


class UserConsent(Base):
    __tablename__ = "user_consents"

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = _user_fk()
    purpose: Mapped[enums.ConsentPurpose] = mapped_column(
        pg_enum(enums.ConsentPurpose, "consent_purpose"), nullable=False
    )
    granted_at: Mapped[dt.datetime] = _created_at()
    revoked_at: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True))
    notice_version: Mapped[str] = mapped_column(sa.String(32), nullable=False)

    __table_args__ = (sa.Index("user_consents_user_idx", "user_id", "purpose"),)


# ------------------------------------------------------------------- inbound


class EmailConnection(Base):
    __tablename__ = "email_connections"

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = _user_fk()
    provider: Mapped[enums.EmailProvider] = mapped_column(
        pg_enum(enums.EmailProvider, "email_provider"), server_default="gmail", nullable=False
    )
    google_email: Mapped[str] = mapped_column(sa.String(320), nullable=False)
    refresh_token_ciphertext: Mapped[str] = mapped_column(sa.Text, nullable=False)
    refresh_token_nonce: Mapped[str] = mapped_column(sa.Text, nullable=False)
    refresh_token_key_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    scopes: Mapped[list[str]] = mapped_column(ARRAY(sa.Text), nullable=False)
    history_id: Mapped[str | None] = mapped_column(sa.String(32))
    watch_expires_at: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True))
    status: Mapped[enums.EmailConnectionStatus] = mapped_column(
        pg_enum(enums.EmailConnectionStatus, "email_connection_status"),
        server_default="active",
        nullable=False,
    )
    last_synced_at: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(sa.Text)
    created_at: Mapped[dt.datetime] = _created_at()
    updated_at: Mapped[dt.datetime] = _created_at()

    __table_args__ = (
        sa.Index(
            "email_connections_user_provider_key",
            "user_id",
            "provider",
            "google_email",
            unique=True,
        ),
        sa.Index("email_connections_watch_idx", "watch_expires_at"),
    )


class OAuthState(Base):
    __tablename__ = "oauth_states"

    state: Mapped[str] = mapped_column(sa.String(128), primary_key=True)
    user_id: Mapped[uuid.UUID] = _user_fk()
    return_path: Mapped[str] = mapped_column(sa.String(256), nullable=False)
    code_verifier: Mapped[str | None] = mapped_column(sa.Text)
    consumed_at: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True))
    expires_at: Mapped[dt.datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    created_at: Mapped[dt.datetime] = _created_at()

    __table_args__ = (sa.Index("oauth_states_expiry_idx", "expires_at"),)


class RawMessage(Base):
    __tablename__ = "raw_messages"

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = _user_fk()
    connection_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("email_connections.id", ondelete="SET NULL")
    )
    provider_msg_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    from_addr: Mapped[str] = mapped_column(sa.String(320), nullable=False)
    subject: Mapped[str | None] = mapped_column(sa.Text)
    received_at: Mapped[dt.datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    kind: Mapped[enums.RawMessageKind] = mapped_column(
        pg_enum(enums.RawMessageKind, "raw_message_kind"), nullable=False
    )
    institution: Mapped[enums.Institution | None] = mapped_column(
        pg_enum(enums.Institution, "institution")
    )
    storage_key: Mapped[str | None] = mapped_column(sa.Text)
    sha256: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    processed_at: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True))
    body_deleted_at: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True))
    created_at: Mapped[dt.datetime] = _created_at()

    __table_args__ = (
        sa.Index("raw_messages_provider_key", "user_id", "provider_msg_id", unique=True),
        sa.Index("raw_messages_sha_key", "user_id", "sha256", unique=True),
        sa.Index("raw_messages_kind_idx", "user_id", "kind", "received_at"),
        sa.Index("raw_messages_retention_idx", "received_at", "body_deleted_at"),
    )


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = _user_fk()
    raw_message_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("raw_messages.id", ondelete="SET NULL")
    )
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("accounts.id", ondelete="SET NULL")
    )
    kind: Mapped[enums.DocumentKind] = mapped_column(
        pg_enum(enums.DocumentKind, "document_kind"),
        server_default="statement_pdf",
        nullable=False,
    )
    filename: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    storage_key: Mapped[str] = mapped_column(sa.Text, nullable=False)
    sha256: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    size_bytes: Mapped[int | None] = mapped_column(sa.Integer)
    encrypted: Mapped[bool] = mapped_column(sa.Boolean, server_default="false", nullable=False)
    institution: Mapped[enums.Institution | None] = mapped_column(
        pg_enum(enums.Institution, "institution")
    )
    status: Mapped[enums.DocumentStatus] = mapped_column(
        pg_enum(enums.DocumentStatus, "document_status"),
        server_default="queued",
        nullable=False,
    )
    parse_engine: Mapped[enums.ParseEngine | None] = mapped_column(
        pg_enum(enums.ParseEngine, "parse_engine")
    )
    error: Mapped[str | None] = mapped_column(sa.Text)
    unlock_attempts: Mapped[int] = mapped_column(sa.Integer, server_default="0", nullable=False)
    processed_at: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True))
    created_at: Mapped[dt.datetime] = _created_at()
    updated_at: Mapped[dt.datetime] = _created_at()

    __table_args__ = (
        sa.Index("documents_sha_key", "user_id", "sha256", unique=True),
        sa.Index("documents_status_idx", "user_id", "status"),
    )


# -------------------------------------------------------------- the ledger


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = _user_fk()
    institution: Mapped[enums.Institution] = mapped_column(
        pg_enum(enums.Institution, "institution"), nullable=False
    )
    type: Mapped[enums.AccountType] = mapped_column(
        pg_enum(enums.AccountType, "account_type"), nullable=False
    )
    masked_number: Mapped[str | None] = mapped_column(sa.String(4))
    nickname: Mapped[str | None] = mapped_column(sa.String(60))
    source: Mapped[enums.AccountSource] = mapped_column(
        pg_enum(enums.AccountSource, "account_source"),
        server_default="detected",
        nullable=False,
    )
    created_at: Mapped[dt.datetime] = _created_at()
    updated_at: Mapped[dt.datetime] = _created_at()

    __table_args__ = (
        sa.CheckConstraint(
            "masked_number is null or masked_number ~ '^[0-9]{4}$'",
            name="accounts_masked_number",
        ),
        sa.Index(
            "accounts_identity_key",
            "user_id",
            "institution",
            "type",
            "masked_number",
            unique=True,
        ),
    )


class Statement(Base):
    __tablename__ = "statements"

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = _user_fk()
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False
    )
    document_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("documents.id", ondelete="SET NULL")
    )
    period_start: Mapped[dt.date] = mapped_column(sa.Date, nullable=False)
    period_end: Mapped[dt.date] = mapped_column(sa.Date, nullable=False)
    opening_balance: Mapped[int | None] = mapped_column(sa.BigInteger)
    closing_balance: Mapped[int | None] = mapped_column(sa.BigInteger)
    reconciled: Mapped[bool] = mapped_column(sa.Boolean, server_default="false", nullable=False)
    reconcile_difference: Mapped[int | None] = mapped_column(sa.BigInteger)
    parse_engine: Mapped[enums.ParseEngine | None] = mapped_column(
        pg_enum(enums.ParseEngine, "parse_engine")
    )
    created_at: Mapped[dt.datetime] = _created_at()
    updated_at: Mapped[dt.datetime] = _created_at()

    __table_args__ = (
        sa.Index("statements_period_key", "account_id", "period_start", "period_end", unique=True),
        sa.Index("statements_user_idx", "user_id", "period_end"),
    )


class Category(Base):
    __tablename__ = "categories"

    id: Mapped[uuid.UUID] = _uuid_pk()
    slug: Mapped[str] = mapped_column(sa.String(64), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("categories.id", ondelete="SET NULL")
    )
    kind: Mapped[enums.CategoryKind] = mapped_column(
        pg_enum(enums.CategoryKind, "category_kind"), nullable=False
    )
    sort_order: Mapped[int] = mapped_column(sa.Integer, server_default="0", nullable=False)

    __table_args__ = (sa.Index("categories_kind_idx", "kind"),)


class Merchant(Base):
    __tablename__ = "merchants"

    id: Mapped[uuid.UUID] = _uuid_pk()
    normalized_name: Mapped[str] = mapped_column(sa.String(120), nullable=False, unique=True)
    aliases: Mapped[list[str]] = mapped_column(
        ARRAY(sa.Text), server_default=sa.text("'{}'::text[]"), nullable=False
    )
    default_category_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("categories.id", ondelete="SET NULL")
    )
    created_at: Mapped[dt.datetime] = _created_at()

    __table_args__ = (sa.Index("merchants_aliases_idx", "aliases", postgresql_using="gin"),)


class Transaction(Base):
    __tablename__ = "transactions"

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = _user_fk()
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False
    )
    statement_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("statements.id", ondelete="SET NULL")
    )
    source: Mapped[enums.TransactionSource] = mapped_column(
        pg_enum(enums.TransactionSource, "transaction_source"), nullable=False
    )
    txn_date: Mapped[dt.date] = mapped_column(sa.Date, nullable=False)
    amount_paise: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    direction: Mapped[enums.Direction] = mapped_column(
        pg_enum(enums.Direction, "direction"), nullable=False
    )
    raw_description: Mapped[str] = mapped_column(sa.Text, nullable=False)
    merchant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("merchants.id", ondelete="SET NULL")
    )
    counterparty_vpa: Mapped[str | None] = mapped_column(sa.String(120))
    counterparty_name: Mapped[str | None] = mapped_column(sa.String(80))
    ref_no: Mapped[str | None] = mapped_column(sa.String(64))
    category_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("categories.id", ondelete="SET NULL")
    )
    category_confidence: Mapped[float | None] = mapped_column(sa.REAL)
    category_source: Mapped[str | None] = mapped_column(sa.String(24))
    is_transfer: Mapped[bool] = mapped_column(sa.Boolean, server_default="false", nullable=False)
    is_recurring: Mapped[bool] = mapped_column(sa.Boolean, server_default="false", nullable=False)
    superseded_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("transactions.id", ondelete="SET NULL")
    )
    fingerprint: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    created_at: Mapped[dt.datetime] = _created_at()
    updated_at: Mapped[dt.datetime] = _created_at()

    __table_args__ = (
        sa.CheckConstraint("amount_paise >= 0", name="transactions_amount_positive"),
        sa.Index("transactions_fingerprint_key", "user_id", "fingerprint", unique=True),
        sa.Index("transactions_ledger_idx", "user_id", "txn_date"),
        sa.Index("transactions_account_idx", "account_id", "txn_date"),
        sa.Index("transactions_category_idx", "user_id", "category_id", "txn_date"),
        sa.Index("transactions_review_idx", "user_id", "category_confidence"),
        sa.Index("transactions_statement_idx", "statement_id"),
        sa.Index("transactions_vpa_idx", "user_id", "counterparty_vpa"),
    )


class UserRule(Base):
    __tablename__ = "user_rules"

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = _user_fk()
    match_kind: Mapped[enums.RuleMatchKind] = mapped_column(
        pg_enum(enums.RuleMatchKind, "rule_match_kind"), nullable=False
    )
    match_value: Mapped[str] = mapped_column(sa.String(160), nullable=False)
    category_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("categories.id", ondelete="CASCADE"), nullable=False
    )
    created_from_txn_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("transactions.id", ondelete="SET NULL")
    )
    created_at: Mapped[dt.datetime] = _created_at()

    __table_args__ = (
        sa.Index("user_rules_match_key", "user_id", "match_kind", "match_value", unique=True),
    )


# ------------------------------------------------------------------- output


class Insight(Base):
    __tablename__ = "insights"

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = _user_fk()
    type: Mapped[enums.InsightType] = mapped_column(
        pg_enum(enums.InsightType, "insight_type"), nullable=False
    )
    title: Mapped[str] = mapped_column(sa.String(120), nullable=False)
    body: Mapped[str] = mapped_column(sa.Text, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, server_default=sa.text("'{}'::jsonb"), nullable=False
    )
    period: Mapped[str | None] = mapped_column(sa.String(7))
    status: Mapped[enums.InsightStatus] = mapped_column(
        pg_enum(enums.InsightStatus, "insight_status"), server_default="new", nullable=False
    )
    dedupe_key: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    notified_at: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True))
    created_at: Mapped[dt.datetime] = _created_at()

    __table_args__ = (
        sa.Index("insights_dedupe_key", "user_id", "dedupe_key", unique=True),
        sa.Index("insights_status_idx", "user_id", "status", "created_at"),
    )


class ReportCache(Base):
    __tablename__ = "report_cache"

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = _user_fk()
    window_months: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    schema_version: Mapped[int] = mapped_column(sa.Integer, server_default="1", nullable=False)
    generated_at: Mapped[dt.datetime] = _created_at()

    __table_args__ = (
        sa.Index("report_cache_user_window_key", "user_id", "window_months", unique=True),
    )


# ------------------------------------------------------------ infrastructure


class Secret(Base):
    __tablename__ = "secrets"

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = _user_fk()
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("accounts.id", ondelete="CASCADE")
    )
    kind: Mapped[enums.SecretKind] = mapped_column(
        pg_enum(enums.SecretKind, "secret_kind"), nullable=False
    )
    ciphertext: Mapped[str] = mapped_column(sa.Text, nullable=False)
    nonce: Mapped[str] = mapped_column(sa.Text, nullable=False)
    key_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    consented_at: Mapped[dt.datetime] = _created_at()
    created_at: Mapped[dt.datetime] = _created_at()

    __table_args__ = (
        sa.Index("secrets_account_kind_key", "user_id", "account_id", "kind", unique=True),
    )


class PushSubscription(Base):
    __tablename__ = "push_subscriptions"

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = _user_fk()
    endpoint: Mapped[str] = mapped_column(sa.Text, nullable=False)
    p256dh: Mapped[str] = mapped_column(sa.Text, nullable=False)
    auth: Mapped[str] = mapped_column(sa.Text, nullable=False)
    user_agent: Mapped[str | None] = mapped_column(sa.String(200))
    last_success_at: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True))
    failure_count: Mapped[int] = mapped_column(sa.Integer, server_default="0", nullable=False)
    created_at: Mapped[dt.datetime] = _created_at()

    __table_args__ = (
        sa.Index("push_subscriptions_endpoint_key", "user_id", "endpoint", unique=True),
    )


class JobsLog(Base):
    __tablename__ = "jobs_log"

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE")
    )
    run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    queue: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    job: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    bull_job_id: Mapped[str | None] = mapped_column(sa.String(128))
    status: Mapped[enums.JobStatus] = mapped_column(
        pg_enum(enums.JobStatus, "job_status"), server_default="queued", nullable=False
    )
    attempt: Mapped[int] = mapped_column(sa.Integer, server_default="1", nullable=False)
    started_at: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True))
    finished_at: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True))
    duration_ms: Mapped[int | None] = mapped_column(sa.Integer)
    error: Mapped[str | None] = mapped_column(sa.Text)
    created_at: Mapped[dt.datetime] = _created_at()

    __table_args__ = (
        sa.Index("jobs_log_user_idx", "user_id", "created_at"),
        sa.Index("jobs_log_run_idx", "run_id"),
    )


class BackfillRun(Base):
    __tablename__ = "backfill_runs"

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = _user_fk()
    connection_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        sa.ForeignKey("email_connections.id", ondelete="CASCADE"),
        nullable=False,
    )
    window_months: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    state: Mapped[str] = mapped_column(sa.String(16), server_default="pending", nullable=False)
    messages_found: Mapped[int] = mapped_column(sa.Integer, server_default="0", nullable=False)
    messages_processed: Mapped[int] = mapped_column(sa.Integer, server_default="0", nullable=False)
    statements_found: Mapped[int] = mapped_column(sa.Integer, server_default="0", nullable=False)
    alerts_found: Mapped[int] = mapped_column(sa.Integer, server_default="0", nullable=False)
    next_page_token: Mapped[str | None] = mapped_column(sa.Text)
    started_at: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True))
    finished_at: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(sa.Text)
    created_at: Mapped[dt.datetime] = _created_at()

    __table_args__ = (sa.Index("backfill_runs_user_idx", "user_id", "created_at"),)


class DataExport(Base):
    __tablename__ = "data_exports"

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = _user_fk()
    status: Mapped[enums.JobStatus] = mapped_column(
        pg_enum(enums.JobStatus, "job_status"), server_default="queued", nullable=False
    )
    include_pdfs: Mapped[bool] = mapped_column(sa.Boolean, server_default="true", nullable=False)
    storage_key: Mapped[str | None] = mapped_column(sa.Text)
    expires_at: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(sa.Text)
    created_at: Mapped[dt.datetime] = _created_at()
    finished_at: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True))

    __table_args__ = (sa.Index("data_exports_user_idx", "user_id", "created_at"),)


class LlmUsage(Base):
    __tablename__ = "llm_usage"

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = _user_fk()
    day: Mapped[dt.date] = mapped_column(sa.Date, nullable=False)
    model: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    prompt_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    input_tokens: Mapped[int] = mapped_column(sa.Integer, server_default="0", nullable=False)
    output_tokens: Mapped[int] = mapped_column(sa.Integer, server_default="0", nullable=False)
    cost_micros: Mapped[int] = mapped_column(sa.BigInteger, server_default="0", nullable=False)
    created_at: Mapped[dt.datetime] = _created_at()

    __table_args__ = (sa.Index("llm_usage_budget_idx", "user_id", "day"),)


#: Tables carrying a tenant's own rows. Every one is RLS-forced; the cross-tenant
#: test iterates this list so a new table cannot be added without being covered.
USER_OWNED_TABLES: tuple[str, ...] = (
    "users",
    "user_consents",
    "email_connections",
    "oauth_states",
    "raw_messages",
    "documents",
    "accounts",
    "statements",
    "transactions",
    "user_rules",
    "insights",
    "secrets",
    "push_subscriptions",
    "jobs_log",
    "backfill_runs",
    "data_exports",
    "llm_usage",
    "report_cache",
)

#: Shared reference data: readable by every tenant, writable only by the worker.
SHARED_TABLES: tuple[str, ...] = ("categories", "merchants")

VIEWS: tuple[str, ...] = (
    "v_publishable_transactions",
    "v_monthly_spend",
    "v_category_spend",
    "v_statement_coverage",
    "v_upi_counterparties",
)
