"""Pulling the payee's name out of bank narration.

Indian statements print the counterparty as an uppercase run inside an
otherwise machine-generated string:

    L/610213183067/ICI910d7cac…/ UPI/GANESAN S/pandim
    NAV/YesBank_Ye/612115254494/ICIe9e7… BISMILLAH M A CHICKEN AND MUTTON CENTER

Measured on real data: 275 of 399 unlabelled rows carry an extractable name,
and those 275 rows are only **118 distinct payees**. That ratio is the whole
reason this module exists — a decision cached per payee compounds across every
month, and it is what makes the model rung (§9 rung 8) affordable.

Nothing here interprets the name. It only finds it.
"""

from __future__ import annotations

import re

#: An uppercase run: the shape a bank prints a counterparty in. Allows the
#: punctuation that appears in Indian trade names (M/S, A.B., O'BRIEN).
UPPERCASE_RUN = re.compile(r"[A-Z][A-Z0-9 &.'/-]{3,48}[A-Z0-9]")

#: Bank and rail vocabulary. These are uppercase too, so without removing them
#: the "payee" comes back as "UPI" or "HDFC BANK" for half the ledger.
NOISE_WORDS = frozenset(
    {
        "UPI",
        "NEFT",
        "IMPS",
        "RTGS",
        "ACH",
        "POS",
        "ATM",
        "NFS",
        "MMT",
        "TPT",
        "INF",
        "BIL",
        "NACH",
        "ECS",
        "EMI",
        "CHQ",
        "CMS",
        "INB",
        "PAY",
        "PAYMENT",
        "TXN",
        "TRF",
        "TRANSFER",
        "REF",
        "MANDATE",
        "AUTOPAY",
        "EXECUTION",
        "BANK",
        "LTD",
        "LIMITED",
        "PRIVATE",
        "PVT",
        "INDIA",
        "BRANCH",
        "ACCOUNT",
        "HDFC",
        "ICICI",
        "SBI",
        "AXIS",
        "KOTAK",
        "YES",
        "YESBANK",
        "BOB",
        "IDFC",
        "DEBIT",
        "CREDIT",
        "CARD",
        "SAL",
        "SALARY",
        "NA",
        "NO",
        "DR",
        "CR",
    }
)

#: A run that is mostly consonants with no vowel is a machine id, not a name
#: (`BLMSHXJMRXU`). Cheap, and it keeps obvious junk out of the model's input.
VOWELS = set("AEIOU")


def _is_wordlike(token: str) -> bool:
    if token in NOISE_WORDS or token.isdigit():
        return False
    if len(token) < 2:
        return False
    letters = [c for c in token if c.isalpha()]
    if not letters:
        return False
    # Letters glued to digits is an identifier, not a trade name: `ICI910` is
    # the head of a reference hash. Short ones survive, for `6E` and `A1`.
    if len(token) >= 4 and any(c.isdigit() for c in token):
        return False
    # A long all-consonant run is an identifier too (`BLMSHXJMRXU`).
    return len(token) <= 3 or bool(VOWELS & set(letters))


def _drop_repeats(tokens: list[str]) -> list[str]:
    """Remove a token the narration already said.

    Banks print the payee twice and truncate the second copy, so
    `BISMILLAH … MUTTON CENTER BISMIL` is one name followed by the start of
    itself. An exact repeat goes; so does a later token that is merely the
    beginning of an earlier one.
    """
    kept: list[str] = []
    for token in tokens:
        if token in kept:
            continue
        if any(earlier.startswith(token) and len(token) >= 4 for earlier in kept):
            continue
        kept.append(token)
    return kept


def extract_payee(description: str | None) -> str | None:
    """The counterparty's printed name, or None.

    Picks the longest uppercase run that survives noise removal — the longest
    because `BISMILLAH M A CHICKEN AND MUTTON CENTER` is more useful than
    `BISMILLAH`, and banks truncate from the right.
    """
    if not description:
        return None

    best: str | None = None
    for match in UPPERCASE_RUN.finditer(description):
        tokens = [t for t in re.split(r"[ /]+", match.group(0)) if t]
        kept = [t.strip(".'-") for t in tokens if _is_wordlike(t.strip(".'-"))]
        kept = [t for t in kept if t]
        if not kept:
            continue
        candidate = " ".join(_drop_repeats(kept))
        if len(candidate) < 4:
            continue
        if best is None or len(candidate) > len(best):
            best = candidate

    return best


def normalise_payee(name: str | None) -> str | None:
    """A stable cache key for one payee.

    Lowercased, punctuation dropped, whitespace collapsed — so `BAJAJ FINA`
    and `BAJAJ FINANCE LTD` at least share a prefix for trigram matching, and
    the same payee printed twice is one key.
    """
    if not name:
        return None
    cleaned = re.sub(r"[^a-z0-9 ]+", " ", name.lower())
    collapsed = " ".join(cleaned.split())
    return collapsed or None


# ------------------------------------------------------------------- rails

#: The payment rail, as banks print it at the head of a narration. This is
#: what a person recognises first — "UPI" and "POS" mean different things
#: about where the money went, and the row reads wrong without it.
RAILS: tuple[tuple[str, str], ...] = (
    (r"\bupi\b", "UPI"),
    (r"\bneft\b", "NEFT"),
    (r"\bimps\b", "IMPS"),
    (r"\brtgs\b", "RTGS"),
    (r"\bnach\b", "NACH"),
    (r"\bach\b", "ACH"),
    (r"\bpos\b", "POS"),
    (r"\b(?:atm|nfs|cash\s+wdl)\b", "ATM"),
    (r"\bbil\b", "BILL"),
    (r"\becs\b", "ECS"),
    (r"\binft\b", "TRANSFER"),
)

_RAILS = tuple((re.compile(pattern, re.IGNORECASE), label) for pattern, label in RAILS)


def extract_rail(description: str | None) -> str | None:
    """Which rail carried this, or None.

    First match wins and the order above is deliberate: a UPI narration often
    also contains the sending bank's `INFT`, and the rail the user cares
    about is the one that moved the money.
    """
    if not description:
        return None
    for pattern, label in _RAILS:
        if pattern.search(description):
            return label
    return None


def row_title(
    *,
    description: str | None,
    merchant_name: str | None,
    counterparty_name: str | None,
    counterparty_vpa: str | None,
) -> str:
    """The one line a person reads to recognise a transaction.

    Written here rather than in the client (§6), so two screens can never
    phrase the same row differently. Shape is `RAIL / who`, falling back
    through the identifiers in order of how much each actually tells someone.
    """
    rail = extract_rail(description)
    who = (
        merchant_name or counterparty_name or counterparty_vpa or extract_payee(description) or None
    )
    if rail and who:
        return f"{rail} / {who}"
    if who:
        return str(who)

    # Nothing names the counterparty. The reference at least tells two rows
    # apart, which matters most on a screen whose job is sorting them — a
    # column of identical "UPI" rows is unusable.
    reference = REFERENCE.search(description or "")
    if rail and reference:
        return f"{rail} · {reference.group(1)}"
    return rail or "Payment"


#: A 12-digit UPI reference, or a long numeric reference banks print.
REFERENCE = re.compile(r"(?<![0-9])([0-9]{9,14})(?![0-9])")
