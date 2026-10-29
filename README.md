# Todd — backend (Python)

Turns bank statements and transaction alerts in a user's Gmail into one combined
spend report across their Indian bank accounts and cards.

A rebuild of the TypeScript service. The build sheet is
[`docs/python-port-spec.md`](../Todd-Backend/docs/python-port-spec.md) in the Node repo —
read §8 *Hard-won lessons* before writing code.

## Three rules that shape the design

1. **Money is integer paise.** Never a float, anywhere, for any reason.
2. **Nothing unverified reaches the ledger.** A statement whose arithmetic does
   not close is held back, not published.
3. **Every stage is idempotent.** The same email, PDF or row arriving twice has
   no effect.

## Setup

Requires [uv](https://docs.astral.sh/uv/). Python 3.12 is installed by uv itself.

```sh
brew install uv
uv sync                 # creates .venv and installs everything
cp .env.example .env    # then fill it in
```

## Commands

```sh
uv run uvicorn todd.api.main:app --reload --port 8080
uv run pytest
uv run ruff check . && uv run ruff format --check .
uv run mypy todd
```

## Layout

```
todd/
  api/        app factory, dependencies, routes, error envelope
  schemas/    Pydantic models — the single source of truth
  db/         SQLAlchemy models, session + RLS scope, seed data
  parsers/    pdf -> table/llm -> reconcile -> dedupe
  worker/     agents (inbox, statement, …) and their libs
  config.py   validated settings, loaded once at boot
migrations/   Alembic; the five views and all RLS policies
scripts/      parse_debug, reprocess, refingerprint, gmail_discover
tools/        the geometry table extractor
tests/
```

## Build order

Each step leaves something working and testable.

| # | Step | Status |
|---|---|---|
| 0 | Project skeleton, tooling | done |
| 1 | Config + logging + health routes | |
| 2 | Database + RLS + the cross-tenant test | |
| 3 | `schemas/` — enums, primitives, money, dates | |
| 4 | Table extractor | |
| 5 | Reconcile + dedupe | |
| 6 | Statement agent | |
| 7 | Crypto + storage | |
| 8 | Gmail ingest | |
| 9 | LLM fallback | |
| 10 | API routes, then diff `openapi.json` against the Node one | |
| 11 | Enrich agent | |

Steps 1–7 need no API keys and no network.

## Non-negotiable

- Account numbers masked to **last 4 everywhere** — UI, logs, LLM prompts.
- **RLS on every user table**, with a test proving cross-tenant isolation.
  Every view declared `WITH (security_invoker = true)`.
- PDF passwords used in memory and **discarded** unless the user asks to remember.
- **Never commit real customer data.** Fixtures are synthesised.
- All secrets in `.env.example` with **no real values**.
