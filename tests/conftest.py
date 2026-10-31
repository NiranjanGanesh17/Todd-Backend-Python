"""Test fixtures.

Tests run against a dedicated `todd_py_test` database, created and migrated on
first use. Never the development database: these tests delete rows.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator, Iterator

import psycopg
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from todd.config import Settings

TEST_DB = "todd_py_test"


def _test_settings() -> Settings:
    base = Settings(_env_file=".env")  # type: ignore[call-arg]
    url = base.database_url.rsplit("/", 1)[0] + f"/{TEST_DB}"
    return base.model_copy(update={"database_url": url, "app_env": "test"})


@pytest.fixture(scope="session")
def settings() -> Settings:
    return _test_settings()


@pytest.fixture(scope="session", autouse=True)
def _migrated_database(settings: Settings) -> Iterator[None]:
    """Create the test database if it is missing, then bring it to head."""
    admin_url = settings.database_url.replace("+psycopg", "").rsplit("/", 1)[0] + "/postgres"
    with psycopg.connect(admin_url, autocommit=True) as conn:
        exists = conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (TEST_DB,)).fetchone()
        if not exists:
            conn.execute(f'CREATE DATABASE "{TEST_DB}"')

    from alembic import command
    from alembic.config import Config

    cfg = Config("alembic.ini")
    os.environ["DATABASE_URL"] = settings.database_url
    command.upgrade(cfg, "head")

    # Categories and the merchant dictionary are reference data the ladder
    # reads on every run, so a test database without them is not the schema
    # the application actually runs against.
    _seed(settings)
    yield


def _seed(settings: Settings) -> None:
    from todd.db.seed.data import SEED_CATEGORIES, SEED_MERCHANTS

    url = settings.database_url.replace("+psycopg", "")
    with psycopg.connect(url, autocommit=True) as conn:
        for category in SEED_CATEGORIES:
            conn.execute(
                "INSERT INTO categories (slug, name, kind, sort_order) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT (slug) DO UPDATE SET "
                "name = EXCLUDED.name, kind = EXCLUDED.kind, "
                "sort_order = EXCLUDED.sort_order",
                (category.slug, category.name, category.kind.value, category.sort_order),
            )
        for merchant in SEED_MERCHANTS:
            conn.execute(
                "INSERT INTO merchants (normalized_name, aliases, default_category_id) "
                "VALUES (%s, %s, (SELECT id FROM categories WHERE slug = %s)) "
                "ON CONFLICT (normalized_name) DO UPDATE SET "
                "aliases = EXCLUDED.aliases, "
                "default_category_id = EXCLUDED.default_category_id",
                (merchant.normalized_name, list(merchant.aliases), merchant.category_slug),
            )


@pytest_asyncio.fixture
async def sessionmaker_(settings: Settings) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(settings.database_url, poolclass=None)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
def user_a() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def user_b() -> uuid.UUID:
    return uuid.uuid4()
