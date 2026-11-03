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
    header_of,
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

#: The shape that made one ICICI account into three. The bank names the
#: account in its own sentence near the top; the narrations below name other
#: numbers, and each month's narrations name different ones.
ICICI_SAVINGS = [
    "ICICI Bank Limited",
    "MR NIRANJAN G",
    "Detailed Statement",
    "01-07-2026 UPI UPI/128760269541/Payment from a/c 4941 2,500.00 1,04,941.00",
    "Statement of Transactions in Savings Account Number: 004501564577",
    "02-07-2026 UPI Transaction charges for A/c 9483 debited 118.00 1,04,823.00",
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


def test_the_banks_own_sentence_names_the_account() -> None:
    """ "Statement of Transactions in Savings Account Number: ..." — the one
    line on the page that says which account the statement is *of*."""
    assert detect_masked_number(ICICI_SAVINGS) == "4577"


def test_a_narration_above_the_header_does_not_win() -> None:
    """Why Profile showed ICICI ··4941, ··4942 and ··9483 for one account.

    Three statements of the same account, each with a different payment at
    the top of its first page, read three different last fours — and the
    natural key includes the number, so each made an account of its own.
    """
    months = [
        "01-07-2026 UPI UPI/1287/Payment from a/c 4941 2,500.00",
        "01-08-2026 UPI Charges for A/c 9483 debited 118.00",
        "01-09-2026 NEFT To a/c no 4942 from salary 85,000.00",
    ]
    sentence = "Statement of Transactions in Savings Account Number: 004501564577"
    assert [detect_masked_number([month, sentence]) for month in months] == ["4577"] * 3


@pytest.mark.parametrize(
    "header",
    [
        pytest.param(
            ["Statement of Transactions in Savings Account Number: 004501564577"],
            id="one line",
        ),
        pytest.param(
            ["Statement of Transactions in Savings", "Account Number: 004501564577"],
            id="wrapped after savings",
        ),
        pytest.param(
            ["Statement of Transactions in Savings Account Number:", "004501564577"],
            id="value on the next line",
        ),
        pytest.param(
            ["Statement of Transactions in Savings Account", "Number:", "004501564577"],
            id="label and value in their own column",
        ),
    ],
)
def test_the_sentence_is_read_however_the_page_breaks_it(header: list[str]) -> None:
    """A PDF has no lines, only words at positions.

    The same printed sentence arrives whole in one statement and split in
    three in the next, depending on where the layout wrapped it. Read line by
    line it names the account in some months and not in others — and a month
    where it goes unread is a month that invents an account from a narration.
    """
    assert detect_masked_number(header) == "4577"


def test_half_a_sentence_names_nothing() -> None:
    """Falling through to the later passes beats taking the next line's digits."""
    assert (
        detect_masked_number(
            [
                "Statement of Transactions in Savings Account",
                "01-07-2026 UPI UPI/1287/Payment from a/c 4941 2,500.00",
            ]
        )
        is None
    )


def test_a_label_with_nothing_under_it_invents_nothing() -> None:
    assert detect_masked_number(["Account Number:", "01-07-2026 UPI 2,500.00"]) is None


def test_the_account_is_not_read_out_of_the_table() -> None:
    """Every line here mentions an account; none of them names this one."""
    assert (
        detect_masked_number(
            [
                "DATE MODE** PARTICULARS DEPOSITS WITHDRAWALS BALANCE",
                "01-07-2026 UPI UPI/1287/Payment from a/c 4941 2,500.00 1,04,941.00",
                "02-07-2026 IMPS To account no 9483 debited 118.00 1,04,823.00",
            ]
        )
        is None
    )


def test_the_period_beside_the_number_is_not_part_of_it() -> None:
    """Banks print the statement period on the same line, and four of its
    digits are a year: taken as part of the number the account reads ··2026
    every month."""
    assert (
        detect_masked_number(
            [
                "Statement of Transactions in Savings Account Number: "
                "004501564577 01-07-2026 to 31-07-2026"
            ]
        )
        == "4577"
    )


def test_a_card_number_printed_in_groups_is_one_number() -> None:
    assert detect_masked_number(["Card Number 4035 XXXX XXXX 1008"]) == "1008"


# ------------------------------------------------- where the header ends


def test_the_header_ends_where_the_table_begins() -> None:
    """Both banks print the account immediately above the column titles."""
    lines = [
        "ICICI Bank Limited",
        "MR NIRANJAN G",
        "Statement of Transactions in Savings Account Number: 004501564577",
        "DATE MODE** PARTICULARS DEPOSITS WITHDRAWALS BALANCE",
        "01-07-2026 UPI UPI/1287/Payment from a/c 4941 2,500.00",
    ]
    assert header_of(lines) == lines[:3]


def test_a_tall_header_is_read_to_its_end() -> None:
    """Forty lines was a guess, and a statement with a long address block or
    a summary box puts the account past it — where it was never read, and a
    narration named the account instead."""
    lines = [
        "ICICI Bank Limited",
        *[f"Registered office line {i}" for i in range(44)],
        "Statement of Transactions in Savings Account Number: 004501564577",
        "DATE MODE** PARTICULARS DEPOSITS WITHDRAWALS BALANCE",
        "01-07-2026 UPI UPI/1287/Payment from a/c 4941 2,500.00",
    ]
    hint = detect_account(lines)
    assert hint.institution is Institution.ICICI
    assert hint.masked_number == "4577"


def test_the_table_is_not_read_for_the_bank_either() -> None:
    """A transfer's narration names the bank it went to. Cutting at the
    table keeps someone else's bank out of the answer."""
    lines = [
        "BANK OF BARODA",
        "A/c No : XXXXXXXX3424",
        "Date Narration Debit Credit Balance",
        *["30-08-2026 UPI/NIRANJAN G/UPI/ICICI BANK/1287 10,000.00"] * 6,
    ]
    hint = detect_account(lines)
    assert hint.institution is Institution.BOB
    assert hint.masked_number == "3424"


def test_a_page_that_opens_with_the_table_still_reads_something() -> None:
    """A continuation page has no header; counting lines is the fallback."""
    lines = ["Date Narration Debit Credit Balance", "A/c No : XXXXXXXX3424"]
    assert header_of(lines) == lines


# ----------------------------------------------------- the bank, by its IFSC


def test_ifsc_names_a_bank_the_enum_has_no_member_for():
    """The six in `Institution` are the six with tuned parsers.

    Everything else used to land on `unknown` and print "Unknown bank". The
    IFSC's first four characters identify the bank exactly, and every Indian
    statement prints one.
    """
    from todd.parsers.institution import detect_institution_name

    assert detect_institution_name(["IFSC Code: IDFB0040101"]) == "IDFC FIRST Bank"
    assert detect_institution_name(["IFSC: KVBL0001234"]) == "Karur Vysya Bank"
    assert detect_institution_name(["ifsc fdrl0001234"]) == "Federal Bank"


def test_ifsc_is_read_from_the_header_only():
    """A narration names other people's banks.

    "UPI/BINDU WINE/XX3657@okbizax/UPI/AXIS BANK/…" is in the table, not the
    header, and `detect_account` only ever passes the header in.
    """
    from todd.parsers.institution import detect_account

    header = [
        "Statement of transactions in Savings Account 58740100001421 in INR",
        "IFSC Code: IDFB0040101   MICR: 560751002",
    ]
    hint = detect_account(header)
    assert hint.institution_name == "IDFC FIRST Bank"
    # The enum stays honest: there is no IDFC member, so it is still unknown.
    assert hint.institution is Institution.UNKNOWN
    assert hint.masked_number == "1421"


def test_an_unlisted_ifsc_names_nothing_rather_than_guessing():
    from todd.parsers.institution import detect_institution_name

    assert detect_institution_name(["IFSC: ZZZZ0001234"]) is None
    assert detect_institution_name(["no code here at all"]) is None


def test_a_header_with_no_bank_in_its_text_still_finds_one():
    """The case that started this: the logo is an image, so the name is not text."""
    from todd.parsers.institution import detect_account, detect_institution

    bare = ["Statement of transactions in Savings Account 58740100001421 in INR"]
    assert detect_institution("\n".join(bare).lower()) is Institution.UNKNOWN
    assert detect_account(bare).institution_name is None
    assert detect_account([*bare, "IFSC: UBIN0812345"]).institution_name == "Union Bank of India"
