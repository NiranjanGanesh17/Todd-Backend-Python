"""Baseline schema: 20 tables, 20 enums, RLS, and the five views.

Executes `migrations/sql/0001_baseline.sql` verbatim rather than re-deriving
the schema in Alembic operations. The SQL is the proven artefact from the
TypeScript repo and carries things autogenerate cannot express: the
`lower(email)` unique index, the GIN index on merchant aliases, the RLS
policies, and `security_invoker = true` on every view.

Revision ID: 0001_baseline
Revises:
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from alembic import op

revision: str = "0001_baseline"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SQL_DIR = Path(__file__).resolve().parents[1] / "sql"


def upgrade() -> None:
    op.execute((SQL_DIR / "0001_baseline.sql").read_text())


def downgrade() -> None:
    # The baseline is the floor. Dropping the schema is `dropdb`, not a
    # migration — a downgrade that silently deletes every tenant's ledger is
    # not a thing worth making one command away.
    raise NotImplementedError("The baseline has no downgrade. Recreate the database instead.")
