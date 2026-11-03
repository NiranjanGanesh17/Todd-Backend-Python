"""A place for the user's own note on one transaction.

The detail screen's "Add a note": the row says `POS 4471 MKT*GROCERY MART`
and only the person who was there knows it was the week's shop. Nothing reads
this but the screen — it is never sent to the model, never matched against,
and never used to categorise, because a note is the one field on the ledger
that is not evidence.

Bounded at 500 characters. A note is a line, not a journal, and an unbounded
`text` column on the biggest table in the schema is a cost with no ceiling.

Revision ID: 0003_transaction_note
Revises: 0002_payee_labels
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from alembic import op

revision: str = "0003_transaction_note"
down_revision: str | None = "0002_payee_labels"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

BASELINE_SQL = Path(__file__).resolve().parents[1] / "sql" / "0001_baseline.sql"

#: `v_publishable_transactions` is `SELECT t.*`, and Postgres expands that to a
#: fixed column list when the view is created. A new column on the table stays
#: invisible through the view until it is replaced — and every read in the
#: service goes through the view, never the table (§5).
VIEWS_START = "CREATE OR REPLACE VIEW public.v_publishable_transactions"

#: Dependants first, or the drop needs a CASCADE that would take them with it.
DEPENDENTS = (
    "v_upi_counterparties",
    "v_category_spend",
    "v_monthly_spend",
    "v_publishable_transactions",
)


def _views() -> str:
    """The baseline's view block, replayed verbatim.

    Sliced out of the baseline rather than copied here: two copies of a view
    definition is one of them going stale, and the one that goes stale is
    always the copy.
    """
    sql = BASELINE_SQL.read_text()
    return sql[sql.index(VIEWS_START) :]


def upgrade() -> None:
    op.execute("ALTER TABLE transactions ADD COLUMN note varchar(500)")
    op.execute(_views())


def downgrade() -> None:
    for view in DEPENDENTS:
        op.execute(f"DROP VIEW IF EXISTS public.{view}")
    op.execute("ALTER TABLE transactions DROP COLUMN IF EXISTS note")
    op.execute(_views())
