"""PII stripping. Runs before a single byte leaves the machine.

§12 is explicit: no names, no full account numbers, no addresses. Rung 8 asks
the model to categorise a *merchant*, so nothing else needs to travel — not
the amount, not the date, not the account, and above all not a private
individual's name.

§8.6 is the trap: a careless PII regex eats the 12-digit UPI reference that
deduplication depends on. Match Aadhaar only when grouped or labelled, and
guard phone and PIN with digit lookarounds.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

#: Aadhaar: only when grouped `1234 5678 9012` or explicitly labelled. A bare
#: 12-digit run is a UPI UTR far more often than it is an Aadhaar number.
AADHAAR = re.compile(
    r"(?<![0-9])(?:\d{4}[ -]\d{4}[ -]\d{4}|(?<=aadhaar[: ])\d{12})(?![0-9])", re.IGNORECASE
)
PAN = re.compile(r"(?<![A-Z0-9])[A-Z]{5}[0-9]{4}[A-Z](?![A-Z0-9])")
EMAIL = re.compile(r"(?<![\w.-])[\w.+-]+@[\w-]+\.[\w.]{2,}(?![\w.-])")
#: Guarded so a 12-digit UTR is not read as a phone number.
PHONE = re.compile(r"(?<![0-9])(?:\+91[ -]?)?[6-9]\d{9}(?![0-9])")
PIN_CODE = re.compile(r"(?<![0-9])[1-9]\d{5}(?![0-9])")
#: A card number, in any of the groupings statements print.
CARD = re.compile(r"(?<![0-9])(?:\d{4}[ -]?){3}\d{4}(?![0-9])")
#: A long account number — but **never exactly 12 digits**. §8.6: a 12-digit
#: run is a UPI UTR far more often than an account number, and eating it
#: destroys the reference that deduplication keys on (§8.10). Indian account
#: numbers that happen to be 12 digits are the acceptable loss here; a broken
#: fingerprint silently duplicates transactions, which is not.
ACCOUNT = re.compile(r"(?<![0-9])(?:\d{9,11}|\d{13,18})(?![0-9])")


@dataclass(slots=True)
class Redaction:
    text: str
    #: What was removed, by kind. Returned so the removal is visible rather
    #: than assumed.
    counts: dict[str, int] = field(default_factory=dict)


def redact(text: str) -> Redaction:
    """Strip PII, reporting what went."""
    counts: dict[str, int] = {}
    out = text

    for label, pattern, replacement in (
        ("aadhaar", AADHAAR, "[AADHAAR]"),
        ("pan", PAN, "[PAN]"),
        ("email", EMAIL, "[EMAIL]"),
        ("card", CARD, "[CARD]"),
        ("phone", PHONE, "[PHONE]"),
        ("account", ACCOUNT, "[ACCOUNT]"),
        ("pin_code", PIN_CODE, "[PIN]"),
    ):
        out, n = pattern.subn(replacement, out)
        if n:
            counts[label] = n

    return Redaction(text=out, counts=counts)


def redact_payee(name: str) -> str:
    """A merchant name, safe to send.

    Only the trading name travels. Anything that looks like a person's
    identity is removed here as a second line of defence — the caller is
    already expected not to send individuals at all.
    """
    return redact(name).text.strip()
