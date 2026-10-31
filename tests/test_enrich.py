"""The categorisation ladder (§9).

Rung order is the contract: the first rung that fires wins, and
`category_source` records which one did.
"""

from __future__ import annotations

import uuid

import pytest
from todd.worker.agents.enrich import Context, classify

CATEGORIES = {
    slug: uuid.uuid4()
    for slug in (
        "transfers_own", "bank_charges", "cash_withdrawal", "income_salary",
        "income_other", "upi_to_people", "food_delivery", "shopping", "subscriptions",
        "investments_sip",
    )
}  # fmt: skip

SWIGGY = uuid.uuid4()


@pytest.fixture
def ctx() -> Context:
    return Context(
        category_ids=CATEGORIES,
        merchant_aliases=[
            ("amazon prime", uuid.uuid4(), "subscriptions"),
            ("swiggy", SWIGGY, "food_delivery"),
            ("amazon", uuid.uuid4(), "shopping"),
        ],
        user_rules={},
        own_masked={"1008"},
    )


def test_an_own_account_transfer_is_not_spending(ctx: Context) -> None:
    label = classify(description="CREDIT CARD PAYMENT", direction="debit", ctx=ctx)
    assert label.category_slug == "transfers_own"
    assert label.is_transfer
    assert label.confidence == 1.0


def test_a_transfer_is_recognised_by_your_own_account_number(ctx: Context) -> None:
    """Paying your own card from savings is not spend, however it is worded."""
    label = classify(description="IMPS TO XXXX1008", direction="debit", ctx=ctx)
    assert label.is_transfer
    assert label.source == "own_transfer"


def test_a_similar_number_that_is_not_yours_is_not_a_transfer(ctx: Context) -> None:
    assert not classify(description="IMPS TO XXXX9999", direction="debit", ctx=ctx).is_transfer


@pytest.mark.parametrize(
    "narration",
    ["AMB CHARGE FOR SEP", "SMS ALERT CHARGES", "GST ON SERVICE CHARGE", "ANNUAL FEE"],
)
def test_bank_charges_are_their_own_category(narration: str, ctx: Context) -> None:
    label = classify(description=narration, direction="debit", ctx=ctx)
    assert label.category_slug == "bank_charges"
    assert label.confidence == 1.0


def test_an_atm_withdrawal_is_structural(ctx: Context) -> None:
    label = classify(description="ATM CASH WDL BANGALORE", direction="debit", ctx=ctx)
    assert label.category_slug == "cash_withdrawal"
    assert label.source == "structural"


def test_salary_is_only_salary_when_it_arrives(ctx: Context) -> None:
    """Direction matters: a debit mentioning salary is not income."""
    assert classify(description="SALARY FOR SEP", direction="credit", ctx=ctx).category_slug == (
        "income_salary"
    )
    assert (
        classify(description="SALARY ADVANCE REPAYMENT", direction="debit", ctx=ctx).category_slug
        != "income_salary"
    )


def test_a_user_rule_outranks_the_dictionary(ctx: Context) -> None:
    ctx.user_rules[("text", "swiggy")] = "shopping"
    label = classify(description="UPI/SWIGGY/order", direction="debit", ctx=ctx)
    assert label.category_slug == "shopping"
    assert label.source == "user_rule"


def test_the_merchant_dictionary_matches_on_a_word(ctx: Context) -> None:
    label = classify(description="UPI/SWIGGY/bundl", direction="debit", ctx=ctx)
    assert label.category_slug == "food_delivery"
    assert label.merchant_id == SWIGGY
    assert label.confidence == 0.9


def test_the_longest_alias_wins(ctx: Context) -> None:
    """ "amazon prime" must beat "amazon", or every subscription is shopping."""
    label = classify(description="AMAZON PRIME MEMBERSHIP", direction="debit", ctx=ctx)
    assert label.category_slug == "subscriptions"


def test_an_alias_does_not_match_inside_a_longer_word(ctx: Context) -> None:
    """ "amazon" must not fire on "amazonia"."""
    label = classify(description="PAYMENT TO AMAZONIA LABS", direction="debit", ctx=ctx)
    assert label.source != "merchant_dict"


def test_a_business_upi_handle_is_identified_but_not_named(ctx: Context) -> None:
    """Rung 6 exists to stop a shop being filed under "UPI to people". It
    does not invent a category, because there is no honest one."""
    label = classify(description="UPI/q172501657@ybl/PAY", direction="debit", ctx=ctx)
    assert label.source == "vpa_business"
    assert label.category_slug is None
    assert label.confidence == 0.6


def test_a_person_upi_handle_becomes_upi_to_people(ctx: Context) -> None:
    label = classify(description="UPI/itsnevatha@okaxis/Rent", direction="debit", ctx=ctx)
    assert label.category_slug == "upi_to_people"
    assert label.counterparty_name == "Itsnevatha"
    assert label.confidence == 0.7


def test_money_received_from_a_person_is_income_not_spend(ctx: Context) -> None:
    label = classify(description="UPI/john.doe@oksbi/refund", direction="credit", ctx=ctx)
    assert label.category_slug == "income_other"


def test_nothing_matched_says_so_rather_than_guessing(ctx: Context) -> None:
    label = classify(description="POS 4035 SOME LOCAL SHOP", direction="debit", ctx=ctx)
    assert label.source == "unmatched"
    assert label.category_slug is None
    assert label.confidence == 0.0


def test_classification_is_deterministic(ctx: Context) -> None:
    """Re-running enrichment must not reshuffle anyone's report."""
    args = {"description": "UPI/SWIGGY/order", "direction": "debit", "ctx": ctx}
    assert classify(**args) == classify(**args)  # type: ignore[arg-type]


# ----------------------------------------------------------- investments


@pytest.mark.parametrize(
    "narration",
    [
        "UPI/Indian Cle/mutualfunds.el/Indian Cle/ICICI Bank",  # a SIP, as ICICI prints it
        "ZERODHA BROKING LTD",
        "NSE CLEARING LIMITED",
        "SIP INSTALMENT APR",
        "SMALLCASE ORDER",
        "ICCLSIP BSE",
        "DEMAT AMC CHARGES",
    ],
)
def test_money_into_an_investment_is_recognised(narration: str, ctx: Context) -> None:
    """Banks name the settlement rail, never the fund — the fund's own name
    appears nowhere in the narration."""
    label = classify(description=narration, direction="debit", ctx=ctx)
    assert label.category_slug == "investments_sip", narration


@pytest.mark.parametrize(
    "narration",
    ["GOSSIP MAGAZINE", "COMFORT INN STAY", "SIPPING LOUNGE", "TRANSFER TO NSEINDIA LTD"],
)
def test_investment_words_do_not_fire_inside_other_words(narration: str, ctx: Context) -> None:
    """`sip` in "gossip" and `mf` in "comfort" are why every token is
    anchored."""
    assert classify(description=narration, direction="debit", ctx=ctx).category_slug != (
        "investments_sip"
    )


def test_money_arriving_from_a_broker_is_not_an_investment(ctx: Context) -> None:
    """A redemption is income, not a purchase. The rung is debit-only."""
    label = classify(description="ZERODHA PAYOUT", direction="credit", ctx=ctx)
    assert label.category_slug != "investments_sip"
