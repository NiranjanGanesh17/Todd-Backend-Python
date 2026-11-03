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

#: How much of the document to read when its table cannot be found. The
#: header is the only part that names the account; narration further down
#: names other people's banks.
HEADER_LINES = 40

#: How far to look for the table before giving up and counting lines instead.
MAX_HEADER_SCAN = 120

#: The words a statement prints across the top of its table. Three on one
#: line is the table starting, and so the header ending — which is where the
#: banks print the account: "in page 1 just before the table starts".
_COLUMN_WORDS = frozenset(
    {
        "amount",
        "balance",
        "chq",
        "credit",
        "debit",
        "deposit",
        "deposits",
        "description",
        "mode",
        "narration",
        "particulars",
        "ref",
        "remarks",
        "txn",
        "withdrawal",
        "withdrawals",
    }
)

_WORDS = re.compile(r"[a-z]+")

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

#: An IFSC code: four letters for the bank, a zero, then six for the branch.
#: Every Indian bank prints one in the account block at the top of a
#: statement, and the first four characters identify the bank with no
#: ambiguity — which is the only thing on the page that does.
#:
#: The marks above are a closed list of six, because those six have parsers
#: tuned to their layouts. The IFSC is how every *other* bank gets named: the
#: logo is an image, `pdfplumber` reads no words out of it, and a statement
#: whose header says "Statement of transactions in Savings Account …" names
#: no bank anywhere in its text. Without this the row reads "Unknown bank",
#: which is a claim about the user's bank rather than about our parser.
_IFSC = re.compile(r"\b([a-z]{4})0[a-z0-9]{6}\b", re.IGNORECASE)

#: Bank by IFSC prefix. Not exhaustive and not meant to be — an unlisted code
#: returns None and the caller falls back, which is the same outcome as
#: before this existed. Public sector, private, small finance, payments,
#: foreign and the larger co-operatives, since a statement can come from any
#: of them.
_IFSC_BANKS: dict[str, str] = {
    "ABHY": "Abhyudaya Co-operative Bank",
    "AIRP": "Airtel Payments Bank",
    "AUBL": "AU Small Finance Bank",
    "BARB": "Bank of Baroda",
    "BDBL": "Bandhan Bank",
    "BKID": "Bank of India",
    "CBIN": "Central Bank of India",
    "CITI": "Citibank",
    "CIUB": "City Union Bank",
    "CNRB": "Canara Bank",
    "COSB": "Cosmos Bank",
    "CSBK": "CSB Bank",
    "DBSS": "DBS Bank India",
    "DCBL": "DCB Bank",
    "DEUT": "Deutsche Bank",
    "DLXB": "Dhanlaxmi Bank",
    "ESFB": "Equitas Small Finance Bank",
    "FDRL": "Federal Bank",
    "FINO": "Fino Payments Bank",
    "HDFC": "HDFC Bank",
    "HSBC": "HSBC",
    "ICIC": "ICICI Bank",
    "IDFB": "IDFC FIRST Bank",
    "IDIB": "Indian Bank",
    "INDB": "IndusInd Bank",
    "IOBA": "Indian Overseas Bank",
    "IPOS": "India Post Payments Bank",
    "JAKA": "Jammu & Kashmir Bank",
    "JSFB": "Jana Small Finance Bank",
    "KARB": "Karnataka Bank",
    "KKBK": "Kotak Mahindra Bank",
    "KVBL": "Karur Vysya Bank",
    "MAHB": "Bank of Maharashtra",
    "NKGS": "NKGSB Co-operative Bank",
    "PSIB": "Punjab & Sind Bank",
    "PUNB": "Punjab National Bank",
    "PYTM": "Paytm Payments Bank",
    "RATN": "RBL Bank",
    "SBIN": "State Bank of India",
    "SBMB": "SBM Bank India",
    "SCBL": "Standard Chartered Bank",
    "SIBL": "South Indian Bank",
    "SRCB": "Saraswat Co-operative Bank",
    "SURY": "Suryoday Small Finance Bank",
    "SVCB": "SVC Co-operative Bank",
    "TMBL": "Tamilnad Mercantile Bank",
    "UBIN": "Union Bank of India",
    "UCBA": "UCO Bank",
    "UJVN": "Ujjivan Small Finance Bank",
    "UTIB": "Axis Bank",
    "YESB": "YES Bank",
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

#: The sentence a bank prints over the account the statement is *for*:
#: "Statement of Transactions in Savings Account Number: 004501564577".
#: Nothing else on the page is this explicit, so nothing else outranks it.
_STATEMENT_OF = re.compile(
    r"statement\s+of\s+(?:transactions?|accounts?)\b[^:]{0,48}?"
    r"\baccount\s*(?:no\.?|number|#)?\s*[:\-]?\s*([0-9Xx*•·\s\-]{0,32})",
    re.IGNORECASE,
)

#: A labelled field: "Account No: 50100123456789", "A/c No : XXXXXXXX3424",
#: "Card Number 4035XXXXXXXX1008". The word for *number* is required — "from
#: a/c 4941" inside a narration is not a labelled field, and leaving it
#: optional is what let a transaction line name the account.
_ACCOUNT_NUMBER = re.compile(
    r"(?:account|a\s*/\s*c|acct|card)\s*(?:no\.?|number|#)\s*[:\-]?\s*"
    r"([0-9Xx*•·\s\-]{0,32})",
    re.IGNORECASE,
)

#: A row from the table rather than the header: it carries a date, or an
#: amount printed to the paise. The account is never named in one.
_TRANSACTION_ROW = re.compile(r"\b\d{1,2}[-/]\d{1,2}[-/]\d{2,4}\b|\b\d[\d,]*\.\d{2}\b")

#: A value sitting under its label rather than beside it, which is how a
#: two-column header comes out of a PDF: "Account Number:" then "004501564577".
_NUMBER_ONLY = re.compile(r"^[0-9Xx*•·\s\-]{6,32}$")

#: One group of an account or card number: 004501564577, XXXXXXXX3424, or
#: each of the four in "4035 XXXX XXXX 1008".
_NUMBER_GROUP = re.compile(r"[0-9Xx*•·\-]{4,}")

#: A date is not part of an account number, and banks print the period right
#: beside it: "Account Number: 004501564577 01-07-2026 to 31-07-2026".
_DATE_TOKEN = re.compile(r"^\d{1,2}[-/]\d{1,2}[-/]\d{2,4}$")

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
    #: The bank's name as the IFSC gives it, for the banks the enum above has
    #: no member for. None when the header prints no IFSC we recognise, and
    #: the enum's own label is then all there is.
    institution_name: str | None = None

    @property
    def identified(self) -> bool:
        return self.institution is not Institution.UNKNOWN


def header_of(lines: Sequence[str]) -> list[str]:
    """The lines above the table, which is the part that names the account.

    Both banks here print the account number at the bottom of the header,
    immediately above the column titles, so where the table starts is where
    this has to stop — a fixed count of lines either cuts the account off in
    a statement with a tall header, or reads narrations in one with a short
    one, and narrations name other people's banks and other people's
    accounts.
    """
    for index, line in enumerate(lines[:MAX_HEADER_SCAN]):
        found = _WORDS.findall(line.lower())
        if len(_COLUMN_WORDS.intersection(found)) >= 3:
            # A page that opens with the table has no header to read; fall
            # back rather than hand the callers nothing.
            if index >= 2:
                return list(lines[:index])
            break
    return list(lines[:HEADER_LINES])


def detect_institution(text: str) -> Institution:
    """The bank whose marks appear most often, or UNKNOWN on a tie of zero."""
    best = Institution.UNKNOWN
    best_score = 0
    for institution, marks in _MARKS.items():
        score = sum(weight * len(re.findall(pattern, text)) for pattern, weight in marks)
        if score > best_score:
            best, best_score = institution, score
    return best


def detect_institution_name(lines: Sequence[str]) -> str | None:
    """The bank's name, read from the IFSC printed in the header.

    Over the header only, and for the same reason `detect_masked_number`
    stays out of the table: a narration names other people's banks, and
    "UPI/BINDU WINE/XX3657@okbizax/UPI/AXIS BANK/…" would make every
    statement an Axis statement.

    The first code wins. A header carries one IFSC — the account's own —
    where it carries one of anything.
    """
    found = _IFSC.search("\n".join(lines))
    return _IFSC_BANKS.get(found.group(1).upper()) if found else None


def detect_account_type(text: str) -> AccountType | None:
    """CREDIT_CARD on two independent card marks, else None for "not stated".

    Two, not one: "credit limit" alone turns up in the terms printed on the
    back of a savings statement, and one phrase is not worth reconciling an
    account the wrong way round for.
    """
    hits = sum(1 for pattern in _CARD_MARKS if re.search(pattern, text))
    return AccountType.CREDIT_CARD if hits >= 2 else None


def _number_run(captured: str) -> str:
    """The number itself, and nothing that merely follows it on the line.

    The groups of one number, up to the first token that is not one: the
    period a bank prints beside the account number is four more digits, and
    taken as part of it the account reads ··2026 every month of 2026.
    """
    run: list[str] = []
    for token in captured.split():
        if not _NUMBER_GROUP.fullmatch(token) or _DATE_TOKEN.match(token):
            break
        run.append(token)
    return "".join(run)


def _last_four(captured: str, follower: str | None) -> str | None:
    """Four digits from a captured field, or from the line under it.

    A header laid out in two columns comes out of the PDF as a label on one
    line and its value on the next, so a label with nothing after it is not
    the same as no account number.
    """
    digits = _DIGITS.findall(_number_run(captured))
    if len(digits) < 4 and follower is not None and _NUMBER_ONLY.match(follower.strip()):
        digits = _DIGITS.findall(_number_run(follower))
    # Between a page reference that happened to follow the word "account" and
    # a run long enough to be two numbers read as one.
    return "".join(digits[-4:]) if 4 <= len(digits) <= 20 else None


def detect_masked_number(lines: Sequence[str]) -> str | None:
    """The last four digits of the account this statement is for, if printed.

    Read in three passes over the whole header, strongest wording first,
    rather than taking whatever the first line happens to offer:

    1. the bank's own sentence — "Statement of Transactions in Savings
       Account Number: ..." — which names the account the statement is of,
    2. a labelled field anywhere in the header, as long as the line is not a
       row of the table,
    3. an unlabelled masked card number, the shape only a PAN has.

    The pass order is the fix for one account appearing three times on
    Profile. Line order used to decide it, and line order is a property of
    the month's transactions: three statements of one ICICI account read
    "from a/c 4941", "A/c 9483" and "a/c 4942" out of three different
    narrations, and each last four made an account of its own.

    Only ever four digits leave this function. §12 — the full number is not
    ours to keep, and the four are what the user recognises the row by.
    """
    followers: list[str | None] = [*lines[1:], None]
    pairs = list(zip(lines, followers, strict=True))

    # Over the header as one piece of text, not line by line. A PDF has no
    # sentences, only words at positions: the same printed line arrives
    # whole, wrapped after "Savings", or split with the number in a column
    # of its own, depending on the month's layout. Matching per line reads
    # the account in some statements and not in others — which is the bug.
    for sentence in _STATEMENT_OF.finditer("\n".join(lines)):
        found = _last_four(sentence.group(1), None)
        if found:
            return found

    for line, follower in pairs:
        # A narration can carry anything, including the words and digits of
        # an account number. The table is not where the account is named.
        if _TRANSACTION_ROW.search(line):
            continue
        field = _ACCOUNT_NUMBER.search(line)
        if field:
            found = _last_four(field.group(1), follower)
            if found:
                return found

    for line, _ in pairs:
        unlabelled = _MASKED_CARD.search(line)
        if unlabelled:
            return unlabelled.group(1)
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
    header = header_of(lines)
    text = "\n".join(header).lower()

    institution = detect_institution(text)
    if institution is Institution.UNKNOWN:
        institution = fallback

    return AccountHint(
        institution=institution,
        account_type=detect_account_type(text) or fallback_type,
        masked_number=detect_masked_number(header),
        institution_name=detect_institution_name(header),
    )
