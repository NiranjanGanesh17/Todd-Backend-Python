"""Cross-tenant isolation.

This is the test the whole data layer exists to pass, and the one that caught
the `security_invoker` leak in the TypeScript version: without that option a
view runs as its owner, RLS is evaluated as the owner, and every tenant's rows
come back through it.

Written before any route, per §13 step 2.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from todd.db.models import USER_OWNED_TABLES, VIEWS
from todd.db.session import with_user


async def _seed_tenant(session: AsyncSession, user_id: uuid.UUID, marker: str) -> uuid.UUID:
    """One user with one account and one transaction, written as the owner."""
    await session.execute(
        text("INSERT INTO users (id, email) VALUES (:id, :email)"),
        {"id": user_id, "email": f"{marker}@example.test"},
    )
    account_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO accounts (id, user_id, institution, type, masked_number) "
            "VALUES (:id, :uid, 'hdfc', 'savings', '1234')"
        ),
        {"id": account_id, "uid": user_id},
    )
    await session.execute(
        text(
            "INSERT INTO transactions "
            "(user_id, account_id, source, txn_date, amount_paise, direction, "
            " raw_description, fingerprint) "
            "VALUES (:uid, :aid, 'statement', :d, 123456, 'debit', :desc, :fp)"
        ),
        {
            "uid": user_id,
            "aid": account_id,
            "d": dt.date(2026, 4, 1),
            "desc": f"{marker} coffee",
            "fp": f"fp-{marker}-{uuid.uuid4().hex[:8]}",
        },
    )
    return account_id


@pytest.fixture
async def two_tenants(
    sessionmaker_: async_sessionmaker[AsyncSession],
    user_a: uuid.UUID,
    user_b: uuid.UUID,
):
    async with sessionmaker_() as s, s.begin():
        await _seed_tenant(s, user_a, "alice")
        await _seed_tenant(s, user_b, "bob")
    yield user_a, user_b
    async with sessionmaker_() as s, s.begin():
        await s.execute(text("DELETE FROM users WHERE id = ANY(:ids)"), {"ids": [user_a, user_b]})


async def test_a_tenant_sees_only_its_own_transactions(
    sessionmaker_: async_sessionmaker[AsyncSession], two_tenants
) -> None:
    user_a, user_b = two_tenants
    async with sessionmaker_() as s, with_user(s, str(user_a)) as scoped:
        rows = (await scoped.execute(text("SELECT user_id FROM transactions"))).scalars().all()

    assert rows, "the tenant must still see its own row"
    assert set(rows) == {user_a}
    assert user_b not in rows


async def test_every_view_is_tenant_scoped(
    sessionmaker_: async_sessionmaker[AsyncSession], two_tenants
) -> None:
    """The `security_invoker` regression test.

    A view declared without it executes as its owner, so this query would
    return the other tenant's rows even though the table policy is correct.
    """
    user_a, user_b = two_tenants
    for view in VIEWS:
        async with sessionmaker_() as s, with_user(s, str(user_a)) as scoped:
            leaked = (
                await scoped.execute(
                    text(f"SELECT count(*) FROM {view} WHERE user_id = :other"),
                    {"other": user_b},
                )
            ).scalar_one()
        assert leaked == 0, f"{view} leaked rows belonging to another tenant"


async def test_publishable_view_still_returns_the_owner_rows(
    sessionmaker_: async_sessionmaker[AsyncSession], two_tenants
) -> None:
    """Guards against 'fixing' the leak by making every view return nothing."""
    user_a, _ = two_tenants
    async with sessionmaker_() as s, with_user(s, str(user_a)) as scoped:
        mine = (
            await scoped.execute(text("SELECT count(*) FROM v_publishable_transactions"))
        ).scalar_one()
    assert mine == 1


async def test_a_tenant_cannot_write_a_row_owned_by_someone_else(
    sessionmaker_: async_sessionmaker[AsyncSession], two_tenants
) -> None:
    """The WITH CHECK half of the policy."""
    user_a, user_b = two_tenants
    async with sessionmaker_() as s:
        with pytest.raises((DBAPIError, ProgrammingError)):
            async with with_user(s, str(user_a)) as scoped:
                await scoped.execute(
                    text(
                        "INSERT INTO accounts (user_id, institution, type, masked_number) "
                        "VALUES (:uid, 'icici', 'savings', '9999')"
                    ),
                    {"uid": user_b},
                )


async def test_no_user_scoped_table_is_readable_without_a_tenant(
    sessionmaker_: async_sessionmaker[AsyncSession], two_tenants
) -> None:
    """`app.user_id` unset resolves to NULL, and NULL never matches a policy,
    so the default is deny rather than allow."""
    async with sessionmaker_() as s, s.begin():
        await s.execute(text("SET LOCAL ROLE app_user"))
        for table in USER_OWNED_TABLES:
            visible = (await s.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one()
            assert visible == 0, f"{table} is readable with no tenant set"


async def test_every_user_owned_table_has_rls_forced(
    sessionmaker_: async_sessionmaker[AsyncSession],
) -> None:
    """A table added later without RLS is the whole failure mode, so assert on
    the catalogue rather than trusting the migration was written correctly."""
    async with sessionmaker_() as s:
        rows = (
            (
                await s.execute(
                    text(
                        "SELECT c.relname FROM pg_class c "
                        "JOIN pg_namespace n ON n.oid = c.relnamespace "
                        "WHERE n.nspname = 'public' AND c.relkind = 'r' "
                        "AND c.relrowsecurity AND c.relforcerowsecurity"
                    )
                )
            )
            .scalars()
            .all()
        )
    assert set(USER_OWNED_TABLES) <= set(rows), (
        f"missing RLS FORCE: {set(USER_OWNED_TABLES) - set(rows)}"
    )
