"""The gate. Both identities, both checks (§7.3, §10)."""

from __future__ import annotations

from dataclasses import dataclass

from todd.parsers.reconcile import check_aggregate, check_running_balance, reconcile


@dataclass(slots=True)
class Row:
    amount_paise: int
    direction: str
    balance_paise: int | None = None


def test_savings_uses_the_asset_identity() -> None:
    rows = [Row(30_000, "debit", 70_000), Row(50_000, "credit", 120_000)]
    result = reconcile(rows, account_type="savings", opening_paise=100_000, closing_paise=120_000)
    assert result.reconciled
    assert result.chain.verdict == "verified"


def test_a_card_uses_the_liability_identity() -> None:
    """On a card the balance is money owed, so a purchase RAISES it."""
    rows = [Row(30_000, "debit"), Row(50_000, "credit")]
    result = reconcile(
        rows, account_type="credit_card", opening_paise=100_000, closing_paise=80_000
    )
    assert result.reconciled


def test_the_wrong_identity_is_out_by_twice_the_net() -> None:
    """§7.3 — applying the savings identity to a card is wrong by exactly
    2 x (debits - credits), which reads like a misread table rather than the
    wrong formula. Pinned so that error cannot come back unnoticed."""
    rows = [Row(30_000, "debit"), Row(50_000, "credit")]
    asset = check_aggregate(
        rows, account_type="savings", opening_paise=100_000, closing_paise=80_000
    )
    liability = check_aggregate(
        rows, account_type="credit_card", opening_paise=100_000, closing_paise=80_000
    )
    assert liability.passed
    assert not asset.passed
    debit_total, credit_total = 30_000, 50_000
    assert asset.expected_closing_paise - liability.expected_closing_paise == 2 * (
        credit_total - debit_total
    )
    assert abs(asset.difference_paise) == abs(2 * (debit_total - credit_total))


def test_the_chain_names_the_row_that_broke() -> None:
    """An aggregate can come out right while a row is misread; the chain
    cannot, and it says where."""
    rows = [
        Row(10_000, "debit", 90_000),
        Row(10_000, "debit", 75_000),  # should be 80,000
        Row(10_000, "debit", 65_000),
    ]
    chain = check_running_balance(rows)
    assert not chain.passed
    assert chain.checked == 2
    assert chain.breaks[0].row == 1
    assert chain.breaks[0].expected_paise == 80_000
    assert chain.breaks[0].printed_paise == 75_000


def test_a_statement_with_no_balance_column_falls_back_to_the_aggregate() -> None:
    """§8.20 — credit cards print no running balance, so the chain is silent
    and the aggregate has to carry the verdict."""
    rows = [Row(30_000, "debit"), Row(20_000, "debit")]
    chain = check_running_balance(rows)
    assert chain.checked == 0
    assert not chain.passed
    assert chain.verdict == "no balance column to check"

    result = reconcile(rows, account_type="credit_card", opening_paise=0, closing_paise=50_000)
    assert result.reconciled


def test_nothing_is_published_without_a_proof() -> None:
    """No running balance and no printed figures: held back, not guessed."""
    rows = [Row(30_000, "debit"), Row(20_000, "debit")]
    result = reconcile(rows, account_type="savings", opening_paise=None, closing_paise=None)
    assert not result.reconciled
    assert result.reason is not None


def test_a_broken_chain_is_held_back_even_when_the_total_happens_to_match() -> None:
    """Two swapped rows leave the aggregate correct and the chain broken.
    The chain must win, otherwise a misread table publishes."""
    rows = [
        Row(10_000, "debit", 90_000),
        Row(30_000, "debit", 70_000),  # printed balance implies 20,000, not 30,000
    ]
    result = reconcile(rows, account_type="savings", opening_paise=100_000, closing_paise=70_000)
    assert not result.reconciled
    assert "balance chain breaks" in (result.reason or "")


def test_a_rounding_of_under_a_rupee_is_tolerated() -> None:
    rows = [Row(30_000, "debit", 70_050)]
    result = reconcile(rows, account_type="savings", opening_paise=100_000, closing_paise=70_050)
    assert result.reconciled


def test_no_rows_never_reconciles() -> None:
    result = reconcile([], account_type="savings", opening_paise=100, closing_paise=100)
    assert not result.reconciled
