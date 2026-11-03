"""The bank's own name, for the banks the enum has no member for.

`accounts.institution` is a Postgres enum of six banks — the six with parsers
tuned to their layouts. Everything else lands on `unknown`, and Profile then
printed "Unknown bank Savings ··1421", which reads as a claim about the user's
bank rather than about our parser.

A statement's header rarely names its bank in text: the logo is an image and
`pdfplumber` reads no words out of it. What it does print is an IFSC, whose
first four characters identify the bank exactly. This column holds the name
that code resolves to, so a bank can be named without a migration per bank
and without pretending the enum knows it.

Nullable on purpose. Null means "no IFSC we recognised", which is the state
every existing row is in, and the readers fall back to the enum's label — the
behaviour before this column existed.

No view replays: nothing in the schema selects `accounts.*`.

Revision ID: 0004_account_institution_name
Revises: 0003_transaction_note
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0004_account_institution_name"
down_revision: str | None = "0003_transaction_note"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE accounts ADD COLUMN institution_name varchar(60)")


def downgrade() -> None:
    op.execute("ALTER TABLE accounts DROP COLUMN IF EXISTS institution_name")
