"""Reading an account's identity off a statement's first page.

The bug these guard against: every statement resolved to one account called
"Unknown bank savings", because nobody read the header and the unlock path
threw away the institution the document already carried.
"""

from __future__ import annotations

import pytest
from todd.parsers.institution import (
    card_from_filename,
    detect_account,
    detect_masked_number,
)
from todd.schemas.enums import AccountType, Institution

HDFC_SAVINGS = [
    "HDFC BANK LIMITED",
    "Statement of Account",
    "Account No: 50100123456789",
    "Statement From: 01/04/2026 To: 30/04/2026",
    "Opening Balance: 25,000.00",
    "01/04/26 UPI/428134567890/PAY/swiggy@ybl 01/04/26 450.00 24,550.00",
]

ICICI_CARD = [
    "ICICI Bank Credit Card Statement",
    "Card Number 4035XXXXXXXX1008",
    "Total Amount Due 12,345.00",
    "Payment Due Date 05/10/2026",
    "Credit Limit 2,00,000.00",
]

BOB_SAVINGS = [
    "BANK OF BARODA",
    "Account Statement",
    "A/c No : XXXXXXXX3424",
    "IFSC: BARB0DBBANG",
]


def test_a_savings_statement_names_its_bank_and_last_four() -> None:
    hint = detect_account(HDFC_SAVINGS)
    assert hint.institution is Institution.HDFC
    assert hint.account_type is AccountType.SAVINGS
    assert hint.masked_number == "6789"


def test_a_card_statement_is_a_card_not_a_savings_account() -> None:
    """It reconciles the other way round, so this is not cosmetic."""
    hint = detect_account(ICICI_CARD)
    assert hint.institution is Institution.ICICI
    assert hint.account_type is AccountType.CREDIT_CARD
    assert hint.masked_number == "1008"


def test_a_masked_number_is_read_through_its_mask() -> None:
    hint = detect_account(BOB_SAVINGS)
    assert hint.institution is Institution.BOB
    assert hint.masked_number == "3424"


def test_the_page_overrules_the_sender() -> None:
    """Mail from one bank can carry another's statement; the page is the account."""
    hint = detect_account(HDFC_SAVINGS, fallback=Institution.ICICI)
    assert hint.institution is Institution.HDFC


def test_a_silent_page_keeps_the_caller_s_hint() -> None:
    hint = detect_account(["Statement", "Period: April 2026"], fallback=Institution.BOB)
    assert hint.institution is Institution.BOB
    assert hint.masked_number is None


def test_nothing_is_invented_when_nothing_is_known() -> None:
    hint = detect_account(["Statement", "Period: April 2026"])
    assert hint.institution is Institution.UNKNOWN
    assert hint.masked_number is None


def test_a_narration_reference_is_not_an_account_number() -> None:
    """§8.6 — a UTR in a transaction line is not the account's identity."""
    assert detect_masked_number(["05/04/26 NEFT CR ACME CORP SALARY N026041234 85,000.00"]) is None


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("4035XXXXXXXX1008_1406814_Retail_Platinum_NORM.pdf", "1008"),
        ("4035xxxxxxxx1008.pdf", "1008"),
        # The underscore is why `\b` could not end this pattern.
        ("5874202609094202483424.pdf", None),
        ("Statement_2026MTH08_964577661.pdf", None),
    ],
)
def test_a_card_number_in_a_filename_is_the_banks_own_writing(
    filename: str, expected: str | None
) -> None:
    assert card_from_filename(filename) == expected
