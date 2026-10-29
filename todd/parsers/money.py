"""`parse_indian_amount` -> integer paise. Never a float.

Money is an `int` from the moment it is read until the moment it is rendered.
A float cannot hold 0.01 exactly, and a statement that reconciles to the paise
is the only evidence that the extraction was correct.

Indian grouping is `1,23,456.78` — the last group is three digits and every
group before it is two — not `123,456.78`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal

#: Currency marks and separators that carry no numeric meaning.
_CURRENCY = re.compile(r"(?:rs\.?|inr|₹|rupees?)", re.IGNORECASE)

#: Indian or plain grouping, with optional paise.
_NUMBER = re.compile(
    r"""^(?P<whole>\d{1,3}(?:,\d{2,3})*|\d+)
         (?:\.(?P<frac>\d{1,2}))?$""",
    re.VERBOSE,
)

#: §8.3 — `1,234.00Cr` has no word boundary between a digit and a letter, so
#: `\bCr\b` never matches. Guard on "not a letter" instead of a word boundary.
CREDIT_MARKER = re.compile(r"(?:^|[^A-Z])(?:DEPOSIT|CREDIT|CR)(?![A-Z])", re.IGNORECASE)
DEBIT_MARKER = re.compile(r"(?:^|[^A-Z])(?:WITHDRAWAL|WITHDRAWN|DEBIT|DR)(?![A-Z])", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class Amount:
    """A parsed amount. `paise` is signed; `marker` is what the bank printed."""

    paise: int
    marker: str | None = None

    @property
    def is_credit(self) -> bool:
        return self.marker == "cr"

    @property
    def is_debit(self) -> bool:
        return self.marker == "dr"


def _marker_of(text: str) -> str | None:
    """`cr`, `dr`, or None. A Dr/Cr suffix is more reliable than column order,
    but less reliable than the balance chain."""
    if CREDIT_MARKER.search(text):
        return "cr"
    if DEBIT_MARKER.search(text):
        return "dr"
    return None


def parse_indian_amount(text: str | None) -> Amount | None:
    """Integer paise, or None when the text is not an amount.

    Returning None rather than 0 matters: a reference number that fails to
    parse must not silently become a zero-rupee row, and `0` is itself a
    legitimate amount.
    """
    if text is None:
        return None

    raw = text.strip()
    if not raw:
        return None

    marker = _marker_of(raw)

    # A parenthesised amount is negative: (1,234.00) is -1234.00.
    negative = raw.startswith("(") and raw.endswith(")")

    # §8.4 — strip currency marks BEFORE touching dots. "Rs.1,234" reduced to
    # ".1,234" and then parsed as a decimal gives 12 paise.
    cleaned = _CURRENCY.sub("", raw)
    cleaned = re.sub(r"[()\s]", "", cleaned)
    cleaned = re.sub(r"(?:dr|cr)$", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^[+]", "", cleaned)

    explicit_sign = cleaned.startswith("-")
    cleaned = cleaned.lstrip("-")

    # Shed the stray leading and trailing dots a currency strip leaves behind,
    # but never a dot that separates paise.
    cleaned = cleaned.strip(".") if cleaned.count(".") != 1 else cleaned.lstrip(".").rstrip(".")
    if cleaned.startswith("."):
        cleaned = cleaned[1:]

    match = _NUMBER.match(cleaned)
    if not match:
        return None

    whole = int(match.group("whole").replace(",", ""))
    frac = (match.group("frac") or "").ljust(2, "0")
    paise = whole * 100 + int(frac)

    if negative or explicit_sign:
        paise = -paise
    return Amount(paise=paise, marker=marker)


def looks_like_amount(text: str | None) -> bool:
    return parse_indian_amount(text) is not None


def format_paise(paise: int, *, symbol: bool = True) -> str:
    """Indian grouping, for display only.

    `Decimal` appears here and nowhere else: rendering is the one place a
    fractional representation is wanted (§8.2).
    """
    sign = "-" if paise < 0 else ""
    rupees, remainder = divmod(abs(paise), 100)
    digits = str(rupees)

    if len(digits) > 3:
        head, tail = digits[:-3], digits[-3:]
        groups: list[str] = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        digits = ",".join([*groups, tail])

    paise_part = f"{Decimal(remainder) / 100:.2f}"[2:]
    prefix = "₹" if symbol else ""
    return f"{sign}{prefix}{digits}.{paise_part}"
