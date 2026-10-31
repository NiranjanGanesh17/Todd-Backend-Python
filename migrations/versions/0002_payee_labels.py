"""Cache one decision per payee, per user.

§9: the labelling unit is the payee, not the transaction — 399 unlabelled
rows were only 118 distinct payees. Without somewhere to keep the verdict,
every enrichment run re-decides them, and rung 8 re-sends and re-pays for
names it has already classified.

Per-user, not shared: a payee is `merchants` only when it is a public brand.
One person's counterparties are their own data and must not cross tenants,
so this table is RLS-forced like every other user-owned table.

Revision ID: 0002_payee_labels
Revises: 0001_baseline
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0002_payee_labels"
down_revision: str | None = "0001_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE user_payee_labels (
            id          uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
            user_id     uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            payee       varchar(160) NOT NULL,
            category_id uuid REFERENCES categories(id) ON DELETE SET NULL,
            source      varchar(24) NOT NULL,
            confidence  real NOT NULL DEFAULT 0,
            created_at  timestamp with time zone DEFAULT now() NOT NULL,
            updated_at  timestamp with time zone DEFAULT now() NOT NULL
        );

        CREATE UNIQUE INDEX user_payee_labels_key
            ON user_payee_labels USING btree (user_id, payee);

        -- Trigram lookup against the merchant dictionary and against past
        -- payees; pg_trgm is enabled in the baseline.
        CREATE INDEX user_payee_labels_trgm
            ON user_payee_labels USING gin (payee gin_trgm_ops);

        ALTER TABLE user_payee_labels ENABLE ROW LEVEL SECURITY;
        ALTER TABLE user_payee_labels FORCE ROW LEVEL SECURITY;
        CREATE POLICY user_payee_labels_owner ON user_payee_labels
            FOR ALL TO app_user
            USING (user_id = public.current_app_user_id())
            WITH CHECK (user_id = public.current_app_user_id());

        GRANT SELECT, INSERT, UPDATE, DELETE ON user_payee_labels TO app_user;
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS user_payee_labels")
