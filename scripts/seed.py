"""Load the category list and merchant dictionary.

Idempotent — safe to re-run after every migration. Reference data is shared
across tenants and is written by the worker, never by a request, so this runs
as the connection owner rather than through `with_user`.

    uv run python scripts/seed.py
"""

from __future__ import annotations

import asyncio
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from sqlalchemy import text
from todd.config import get_settings
from todd.db.seed.data import SEED_CATEGORIES, SEED_MERCHANTS
from todd.db.session import dispose_engine, get_sessionmaker, init_engine


async def seed() -> None:
    init_engine(get_settings())
    async with get_sessionmaker()() as session, session.begin():
        for category in SEED_CATEGORIES:
            await session.execute(
                text(
                    "INSERT INTO categories (slug, name, kind, sort_order) "
                    "VALUES (:slug, :name, :kind, :sort) "
                    "ON CONFLICT (slug) DO UPDATE SET "
                    "  name = EXCLUDED.name, kind = EXCLUDED.kind, "
                    "  sort_order = EXCLUDED.sort_order"
                ),
                {
                    "slug": category.slug,
                    "name": category.name,
                    "kind": category.kind.value,
                    "sort": category.sort_order,
                },
            )

        for merchant in SEED_MERCHANTS:
            await session.execute(
                text(
                    "INSERT INTO merchants (normalized_name, aliases, default_category_id) "
                    "VALUES (:name, :aliases, (SELECT id FROM categories WHERE slug = :slug)) "
                    "ON CONFLICT (normalized_name) DO UPDATE SET "
                    "  aliases = EXCLUDED.aliases, "
                    "  default_category_id = EXCLUDED.default_category_id"
                ),
                {
                    "name": merchant.normalized_name,
                    "aliases": list(merchant.aliases),
                    "slug": merchant.category_slug,
                },
            )

        counts = (
            (
                await session.execute(
                    text(
                        "SELECT (SELECT count(*) FROM categories) AS categories, "
                        "       (SELECT count(*) FROM merchants) AS merchants"
                    )
                )
            )
            .mappings()
            .one()
        )

    await dispose_engine()
    print(f"seeded: {counts['categories']} categories, {counts['merchants']} merchants")


if __name__ == "__main__":
    asyncio.run(seed())
