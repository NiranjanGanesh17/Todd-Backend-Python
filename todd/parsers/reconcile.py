"""The gate. Nothing that fails it reaches the ledger.

Two independent checks. The TypeScript version only ever ran the first; the
second was written and never called.

**Aggregate** — within ₹1, using the identity that matches what the balance
*means*:

    savings / current    opening + credits − debits = closing
    credit card / loan   opening + debits − credits = closing

On a savings account the balance is money you hold, so a debit lowers it. On a
card it is money you owe, so a purchase raises it. Applying the savings
identity to a card is wrong by exactly `2 × (debits − credits)`, which looks
like a misread table rather than the wrong formula — the most expensive kind of
bug to chase.

**Balance chain** — where a running balance is printed, every consecutive pair
must differ by exactly that row's amount. Strictly stronger: an aggregate can
come out right while two rows are swapped or one is read into the wrong column,
whereas the chain breaks at the first bad row and names it.

The opening balance is never derived from the rows. That would make
reconciliation circular and prove nothing: take opening and closing as
*printed*, and let the chain be the proof.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from itertools import pairwise
from typing import Protocol

from todd.schemas.enums import AccountType

#: ₹1. Statements round; we do not chase the last paise of a rounding.
TOLERANCE_PAISE = 100

#: Account types whose balance is money owed, not money held.
LIABILITY_TYPES = frozenset({AccountType.CREDIT_CARD, AccountType.LOAN})


class Row(Protocol):
    """What the gate needs of a parsed row, whatever produced it."""

    amount_paise: int
    direction: str
    balance_paise: int | None


@dataclass(frozen=True, slots=True)
class AggregateCheck:
    passed: bool
    expected_closing_paise: int
    printed_closing_paise: int
    difference_paise: int
    identity: str

    @property
    def detail(self) -> str:
        return (
            f"{self.identity}: expected closing {self.expected_closing_paise}, "
            f"statement printed {self.printed_closing_paise} "
            f"(out by {self.difference_paise} paise)"
        )


@dataclass(frozen=True, slots=True)
class ChainBreak:
    """A row whose printed balance does not follow from the one above it."""

    row: int
    expected_paise: int
    printed_paise: int
    difference_paise: int


@dataclass(slots=True)
class ChainCheck:
    checked: int
    breaks: list[ChainBreak] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        """A chain with nothing to check has not passed; it is silent.

        Credit cards print no running balance, so `checked == 0` is normal for
        them and the aggregate has to carry the verdict (§8.20).
        """
        return self.checked > 0 and not self.breaks

    @property
    def verdict(self) -> str:
        if not self.checked:
            return "no balance column to check"
        return "verified" if not self.breaks else "broken"


@dataclass(slots=True)
class Reconciliation:
    reconciled: bool
    aggregate: AggregateCheck | None
    chain: ChainCheck
    difference_paise: int | None
    reason: str | None = None


def is_liability(account_type: AccountType | str) -> bool:
    try:
        return AccountType(account_type) in LIABILITY_TYPES
    except ValueError:
        return False


def totals_of(rows: Sequence[Row]) -> tuple[int, int]:
    """(debits, credits) in paise. Named `*_total` because `credits` is a
    Python builtin."""
    debit_total = sum(r.amount_paise for r in rows if r.direction == "debit")
    credit_total = sum(r.amount_paise for r in rows if r.direction == "credit")
    return debit_total, credit_total


def check_aggregate(
    rows: Sequence[Row],
    *,
    account_type: AccountType | str,
    opening_paise: int,
    closing_paise: int,
    tolerance_paise: int = TOLERANCE_PAISE,
) -> AggregateCheck:
    """Does the statement's own arithmetic close?

    `opening_paise` and `closing_paise` must be the figures the statement
    *printed*. Deriving either from the rows makes this check circular.
    """
    debit_total, credit_total = totals_of(rows)

    if is_liability(account_type):
        expected = opening_paise + debit_total - credit_total
        identity = "opening + debits − credits = closing (liability)"
    else:
        expected = opening_paise + credit_total - debit_total
        identity = "opening + credits − debits = closing (asset)"

    difference = expected - closing_paise
    return AggregateCheck(
        passed=abs(difference) <= tolerance_paise,
        expected_closing_paise=expected,
        printed_closing_paise=closing_paise,
        difference_paise=difference,
        identity=identity,
    )


def check_running_balance(
    rows: Sequence[Row], *, tolerance_paise: int = TOLERANCE_PAISE
) -> ChainCheck:
    """Every consecutive pair of printed balances differs by that row's amount.

    The equivalent of this was written in the Node repo and never called. It is
    the check that names the offending row instead of reporting a total that is
    merely wrong somewhere.
    """
    checked = 0
    breaks: list[ChainBreak] = []

    for index, (previous, row) in enumerate(pairwise(rows), start=1):
        if previous.balance_paise is None or row.balance_paise is None:
            continue
        checked += 1
        signed = -row.amount_paise if row.direction == "debit" else row.amount_paise
        expected = previous.balance_paise + signed
        difference = expected - row.balance_paise
        if abs(difference) > tolerance_paise:
            breaks.append(
                ChainBreak(
                    row=index,
                    expected_paise=expected,
                    printed_paise=row.balance_paise,
                    difference_paise=difference,
                )
            )

    return ChainCheck(checked=checked, breaks=breaks)


def reconcile(
    rows: Sequence[Row],
    *,
    account_type: AccountType | str,
    opening_paise: int | None,
    closing_paise: int | None,
    tolerance_paise: int = TOLERANCE_PAISE,
) -> Reconciliation:
    """Run both checks and decide whether this statement may be published.

    A statement passes on either proof: a verified balance chain, or an
    aggregate that closes against the printed figures. Requiring both would
    reject every credit card, which prints no running balance; requiring
    neither would publish anything.
    """
    chain = check_running_balance(rows, tolerance_paise=tolerance_paise)

    aggregate: AggregateCheck | None = None
    if opening_paise is not None and closing_paise is not None:
        aggregate = check_aggregate(
            rows,
            account_type=account_type,
            opening_paise=opening_paise,
            closing_paise=closing_paise,
            tolerance_paise=tolerance_paise,
        )

    if not rows:
        return Reconciliation(
            reconciled=False,
            aggregate=aggregate,
            chain=chain,
            difference_paise=None,
            reason="no rows to reconcile",
        )

    if chain.passed:
        return Reconciliation(reconciled=True, aggregate=aggregate, chain=chain, difference_paise=0)

    if aggregate is not None and aggregate.passed:
        return Reconciliation(
            reconciled=True,
            aggregate=aggregate,
            chain=chain,
            difference_paise=aggregate.difference_paise,
        )

    # Held back. The registry still returns the closest attempt so a human sees
    # real rows and a real difference — but it stays unpublishable.
    if chain.breaks:
        first = chain.breaks[0]
        reason = (
            f"balance chain breaks at row {first.row}: expected "
            f"{first.expected_paise} paise, statement printed {first.printed_paise}"
        )
        difference: int | None = first.difference_paise
    elif aggregate is not None:
        reason = aggregate.detail
        difference = aggregate.difference_paise
    else:
        reason = "no running balance and no printed opening/closing to check against"
        difference = None

    return Reconciliation(
        reconciled=False,
        aggregate=aggregate,
        chain=chain,
        difference_paise=difference,
        reason=reason,
    )
