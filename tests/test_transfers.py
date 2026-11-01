"""Money moved between the user's own accounts, seen from both sides.

One transfer arrives as two rows — a debit on the account it left, a credit on
the account it reached. Left alone they inflate both spending and income by the
same amount, and the report shows the same ten thousand rupees twice.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from todd.db.session import with_user
from todd.worker.agents.enrich import load_context, pair_own_transfers

pytestmark = pytest.mark.asyncio

#: The reference both banks printed on the transfer in the statements that
#: prompted this: a BoB debit and an ICICI credit, same day, same amount.
UTR = "128760269541"

BOB_DEBIT = f"UPI/{UTR}/20:17:47/UPI/niranjanmech17-2@okicici"
ICICI_CREDIT = (
    f"NIRANJAN G UPI/NIRANJAN G/niranjanmech17/UPI/BANK OF BA/{UTR}/HDF193671a6e0f34a31b00"
)


async def _account(session: AsyncSession, user_id: uuid.UUID, institution: str) -> uuid.UUID:
    account_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO accounts (id, user_id, institution, type) "
            "VALUES (:id, :uid, :inst, 'savings')"
        ),
        {"id": account_id, "uid": user_id, "inst": institution},
    )
    return account_id


async def _txn(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    account_id: uuid.UUID,
    day: int,
    amount: int,
    direction: str,
    description: str,
) -> uuid.UUID:
    txn_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO transactions "
            "(id, user_id, account_id, source, txn_date, amount_paise, direction, "
            " raw_description, fingerprint) "
            "VALUES (:id, :uid, :aid, 'statement', :d, :amt, :dir, :desc, :fp)"
        ),
        {
            "id": txn_id,
            "uid": user_id,
            "aid": account_id,
            "d": dt.date(2026, 8, day),
            "amt": amount,
            "dir": direction,
            "desc": description,
            "fp": uuid.uuid4().hex,
        },
    )
    return txn_id


async def _is_transfer(session: AsyncSession, txn_id: uuid.UUID) -> bool:
    return bool(
        (
            await session.execute(
                text("SELECT is_transfer FROM transactions WHERE id = :id"), {"id": txn_id}
            )
        ).scalar_one()
    )


@pytest.fixture
async def tenant(
    sessionmaker_: async_sessionmaker[AsyncSession],
) -> uuid.UUID:
    user_id = uuid.uuid4()
    async with sessionmaker_() as session, session.begin():
        await session.execute(
            text("INSERT INTO users (id, email) VALUES (:id, :email)"),
            {"id": user_id, "email": f"{user_id}@example.test"},
        )
    return user_id


async def test_both_legs_of_one_transfer_stop_counting(
    sessionmaker_: async_sessionmaker[AsyncSession], tenant: uuid.UUID
) -> None:
    async with sessionmaker_() as session, session.begin():
        bob = await _account(session, tenant, "bob")
        icici = await _account(session, tenant, "icici")
        debit = await _txn(
            session,
            user_id=tenant,
            account_id=bob,
            day=30,
            amount=1_000_000,
            direction="debit",
            description=BOB_DEBIT,
        )
        credit = await _txn(
            session,
            user_id=tenant,
            account_id=icici,
            day=30,
            amount=1_000_000,
            direction="credit",
            description=ICICI_CREDIT,
        )

    async with sessionmaker_() as session, session.begin(), with_user(session, str(tenant)) as s:
        ctx = await load_context(s)
        assert await pair_own_transfers(s, user_id=str(tenant), ctx=ctx) == 2

    async with sessionmaker_() as session, session.begin():
        assert await _is_transfer(session, debit)
        assert await _is_transfer(session, credit)
        # Neither side reaches the report's spend or income.
        counted = (
            await session.execute(
                text(
                    "SELECT COALESCE(sum(spent_paise), 0) + COALESCE(sum(received_paise), 0) "
                    "FROM v_monthly_spend WHERE user_id = :uid"
                ),
                {"uid": tenant},
            )
        ).scalar_one()
        assert counted == 0


async def test_the_same_amount_on_the_same_day_is_not_a_transfer(
    sessionmaker_: async_sessionmaker[AsyncSession], tenant: uuid.UUID
) -> None:
    """The rule amount and direction alone would have given.

    Over six months of real statements it paired rent with a refund, and a
    cash withdrawal with a friend repaying a loan: 22 pairs, 1 of them real.
    Two unrelated payments of the same size on the same day stay unrelated.
    """
    async with sessionmaker_() as session, session.begin():
        bob = await _account(session, tenant, "bob")
        icici = await _account(session, tenant, "icici")
        rent = await _txn(
            session,
            user_id=tenant,
            account_id=bob,
            day=30,
            amount=1_000_000,
            direction="debit",
            description="UPI/NEVATHA/itsnevatha@okaxis/Rent/ICICI/610773315720",
        )
        refund = await _txn(
            session,
            user_id=tenant,
            account_id=icici,
            day=30,
            amount=1_000_000,
            direction="credit",
            description="UPI/AKRAM MOHAMMED/7904077885@axl/Payment/646128894010",
        )

    async with sessionmaker_() as session, session.begin(), with_user(session, str(tenant)) as s:
        ctx = await load_context(s)
        assert await pair_own_transfers(s, user_id=str(tenant), ctx=ctx) == 0

    async with sessionmaker_() as session, session.begin():
        assert not await _is_transfer(session, rent)
        assert not await _is_transfer(session, refund)


async def test_a_pair_with_a_correction_in_it_is_left_alone(
    sessionmaker_: async_sessionmaker[AsyncSession], tenant: uuid.UUID
) -> None:
    async with sessionmaker_() as session, session.begin():
        bob = await _account(session, tenant, "bob")
        icici = await _account(session, tenant, "icici")
        debit = await _txn(
            session,
            user_id=tenant,
            account_id=bob,
            day=30,
            amount=1_000_000,
            direction="debit",
            description=BOB_DEBIT,
        )
        credit = await _txn(
            session,
            user_id=tenant,
            account_id=icici,
            day=30,
            amount=1_000_000,
            direction="credit",
            description=ICICI_CREDIT,
        )
        await session.execute(
            text(
                "UPDATE transactions SET category_source = 'user_rule', "
                "  category_id = (SELECT id FROM categories WHERE slug = 'shopping') "
                "WHERE id = :id"
            ),
            {"id": debit},
        )

    async with sessionmaker_() as session, session.begin(), with_user(session, str(tenant)) as s:
        ctx = await load_context(s)
        # Neither leg moves. Filing one side as shopping and the other as a
        # transfer between your own accounts are two contradictory claims
        # about one payment, and the one the user made out loud wins.
        assert await pair_own_transfers(s, user_id=str(tenant), ctx=ctx) == 0

    async with sessionmaker_() as session, session.begin():
        assert not await _is_transfer(session, debit)
        assert not await _is_transfer(session, credit)


async def test_pairing_twice_changes_nothing(
    sessionmaker_: async_sessionmaker[AsyncSession], tenant: uuid.UUID
) -> None:
    """It runs at the end of every parse, so it has to be idempotent."""
    async with sessionmaker_() as session, session.begin():
        bob = await _account(session, tenant, "bob")
        icici = await _account(session, tenant, "icici")
        await _txn(
            session,
            user_id=tenant,
            account_id=bob,
            day=30,
            amount=1_000_000,
            direction="debit",
            description=BOB_DEBIT,
        )
        await _txn(
            session,
            user_id=tenant,
            account_id=icici,
            day=30,
            amount=1_000_000,
            direction="credit",
            description=ICICI_CREDIT,
        )

    async with sessionmaker_() as session, session.begin(), with_user(session, str(tenant)) as s:
        ctx = await load_context(s)
        assert await pair_own_transfers(s, user_id=str(tenant), ctx=ctx) == 2
        assert await pair_own_transfers(s, user_id=str(tenant), ctx=ctx) == 0
