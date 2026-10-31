"""Whose account is this statement for?

The ledger does not need to know, but Profile does: an account row that says
"Unknown bank savings ··" is the one line on that screen the user cannot act
on, and it is what every statement collapses into when nobody looks.

Three things come out of a statement's first page, and all three come from the
printed text rather than from the filename or the sender:

* the **institution**, from the bank's own name and its IFSC prefix,
* the **account type**, because a credit card statement is a liability and
  reconciles the other way round (§8.4),
* the **last four digits**, which is all we keep of an account number (§12).

Nothing here guesses. An unreadable header returns UNKNOWN and a null number,
which Profile renders honestly, and a caller's own hint — the Gmail sender,
say — stays in charge when the page says nothing.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from todd.schemas.enums import AccountType, Institution

#: How much of the document to read. The header is the only part that names
#: the account; narration further down names other people's banks.
HEADER_LINES = 40

#: Brand names and IFSC prefixes. The IFSC is the stronger signal — it is
#: structural, printed once, and cannot appear in someone else's narration —
#: so it counts for more.
_MARKS: dict[Institution, tuple[tuple[str, int], ...]] = {
    Institution.HDFC: ((r"\bhdfc\s+bank\b", 2), (r"\bhdfc0\d{6}\b", 4), (r"\bhdfc\b", 1)),
    Institution.ICICI: ((r"\bicici\s+bank\b", 2), (r"\bicic0\d{6}\b", 4), (r"\bicici\b", 1)),
    Institution.SBI: (
        (r"\bstate\s+bank\s+of\s+india\b", 2),
        (r"\bsbin0\d{6}\b", 4),
        (r"\bsbi\b", 1),
    ),
    Institution.AXIS: ((r"\baxis\s+bank\b", 2), (r"\butib0\d{6}\b", 4), (r"\baxis\b", 1)),
    Institution.KOTAK: (
        (r"\bkotak\s+mahindra\b", 2),
        (r"\bkkbk0\d{6}\b", 4),
        (r"\bkotak\b", 1),
    ),
    Institution.BOB: (
        (r"\bbank\s+of\s+baroda\b", 2),
        (r"\bbarb0\w{6}\b", 4),
        (r"\bbobcard\b", 2),
        (r"\bbaroda\b", 1),
    ),
}

#: Phrases that only a card statement prints. A savings statement has a
#: closing balance; a card has an amount due and a limit.
_CARD_MARKS: tuple[str, ...] = (
    r"\bcredit\s+card\s+statement\b",
    r"\bminimum\s+amount\s+due\b",
    r"\btotal\s+amount\s+due\b",
    r"\bpayment\s+due\s+date\b",
    r"\bcredit\s+limit\b",
    r"\bavailable\s+credit\b",
    r"\bstatement\s+date\b.*\bdue\s+date\b",
)

#: A labelled account or card number, and nothing else. Keyed on the label so
#: a reference number in a narration line can never be mistaken for one.
_ACCOUNT_NUMBER = re.compile(
    r"(?:account|a\s*/\s*c|acct|card)\s*(?:no\.?|number|#)?\s*[:\-]?\s*"
    r"([0-9Xx*•·\s\-]{6,32})",
    re.IGNORECASE,
)

#: Card numbers are often printed with no label at all: 4035XXXXXXXX1008.
#: Not `\b` at the end: an underscore is a word character, so the number in
#: `4035XXXXXXXX1008_1406814_Retail.pdf` would never close on a boundary.
_MASKED_CARD = re.compile(r"(?<![0-9])\d{4}[Xx*•]{4,12}(\d{4})(?![0-9])")

_DIGITS = re.compile(r"\d")


@dataclass(frozen=True, slots=True)
class AccountHint:
    """What the first page says about the account it belongs to."""

    institution: Institution
    account_type: AccountType
    #: Last four digits, or None when the page prints no account number.
    masked_number: str | None

    @property
    def identified(self) -> bool:
        return self.institution is not Institution.UNKNOWN


def detect_institution(text: str) -> Institution:
    """The bank whose marks appear most often, or UNKNOWN on a tie of zero."""
    best = Institution.UNKNOWN
    best_score = 0
    for institution, marks in _MARKS.items():
        score = sum(weight * len(re.findall(pattern, text)) for pattern, weight in marks)
        if score > best_score:
            best, best_score = institution, score
    return best


def detect_account_type(text: str) -> AccountType | None:
    """CREDIT_CARD on two independent card marks, else None for "not stated".

    Two, not one: "credit limit" alone turns up in the terms printed on the
    back of a savings statement, and one phrase is not worth reconciling an
    account the wrong way round for.
    """
    hits = sum(1 for pattern in _CARD_MARKS if re.search(pattern, text))
    return AccountType.CREDIT_CARD if hits >= 2 else None


def detect_masked_number(lines: Sequence[str]) -> str | None:
    """The last four digits of the account or card number, if one is printed.

    Only ever four digits leave this function. §12 — the full number is not
    ours to keep, and the four are what the user recognises the row by.
    """
    for line in lines:
        unlabelled = _MASKED_CARD.search(line)
        if unlabelled:
            return unlabelled.group(1)

        match = _ACCOUNT_NUMBER.search(line)
        if not match:
            continue
        digits = _DIGITS.findall(match.group(1))
        # Between a page reference that happened to follow the word "account"
        # and a run long enough to be two numbers read as one.
        if 4 <= len(digits) <= 20:
            return "".join(digits[-4:])
    return None


def card_from_filename(filename: str) -> str | None:
    """Last four of a masked card number the bank printed in the filename.

    `4035XXXXXXXX1008_1406814_Retail_Platinum_NORM.pdf` — ICICI names card
    statements after the card. It is not a guess: a masked PAN is a shape no
    savings account number has, and it is the only identity available for a
    statement still locked behind a password.
    """
    found = _MASKED_CARD.search(filename)
    return found.group(1) if found else None


def detect_account(
    lines: Sequence[str],
    *,
    fallback: Institution = Institution.UNKNOWN,
    fallback_type: AccountType = AccountType.SAVINGS,
) -> AccountHint:
    """Read the header for the institution, the type and the last four digits.

    `fallback` is what the caller already believed — the Gmail sender's bank,
    usually. The page overrules it when it names a bank of its own, because
    the page is the account and the sender is only how it arrived.
    """
    header = list(lines[:HEADER_LINES])
    text = "\n".join(header).lower()

    institution = detect_institution(text)
    if institution is Institution.UNKNOWN:
        institution = fallback

    return AccountHint(
        institution=institution,
        account_type=detect_account_type(text) or fallback_type,
        masked_number=detect_masked_number(header),
    )
