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
uv run uvicorn todd.api.main:app --reload --port 4000   # the web app points here
uv run alembic upgrade head                             # schema, RLS, views
uv run python scripts/export_openapi.py                 # regenerate openapi.json
uv run pytest
uv run ruff check . && uv run ruff format --check .
uv run mypy todd
```

### Local infrastructure

Postgres and Redis come from the Node repo's compose file; this service uses a
separate database on the same instance so Alembic and drizzle never collide.

```sh
docker compose -f ../Todd-Backend/infra/docker-compose.yml up -d
docker exec todd-postgres createdb -U todd todd_py
```

### Running against the web app

`Todd-Frontend` expects the API on **port 4000** with `NEXT_PUBLIC_USE_MOCKS=false`.
Stop the Node service first — the two cannot share the port.

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
| 1 | Config + logging + health routes | done |
| 2 | Database + RLS + the cross-tenant test | done |
| 3 | `schemas/` — enums, primitives, money, dates | |
| 4 | Table extractor | |
| 5 | Reconcile + dedupe | |
| 6 | Statement agent | |
| 7 | Crypto + storage | |
| 8 | Gmail ingest | |
| 9 | LLM fallback | |
| 10 | API routes, then diff `openapi.json` against the Node one | read routes done; writes are 501 |
| 11 | Enrich agent | |

Steps 1–7 need no API keys and no network.

## What answers today

| Route | |
|---|---|
| `GET /healthz`, `GET /readyz` | liveness; readiness probes Postgres and Redis |
| `GET /v1/report` | the Home screen, from the `security_invoker` views |
| `GET /v1/profile` | plan, accounts, Gmail connection |
| `GET /v1/goals` | locked Pro tab, reads `GOALS_ENABLED` / `PRO_LOCK_MODE` |
| `GET /v1/gmail/status` | connection plus backfill progress |
| `GET /v1/documents` | stored statements and their status |

The reads are real queries; with no statements ingested they correctly report
an empty system. The writes — upload, unlock, Gmail OAuth — answer `501` with
the standard error envelope until steps 6 to 8 land, rather than accepting a
PDF into a queue that cannot drain.

## Non-negotiable

- Account numbers masked to **last 4 everywhere** — UI, logs, LLM prompts.
- **RLS on every user table**, with a test proving cross-tenant isolation.
  Every view declared `WITH (security_invoker = true)`.
- PDF passwords used in memory and **discarded** unless the user asks to remember.
- **Never commit real customer data.** Fixtures are synthesised.
- All secrets in `.env.example` with **no real values**.
