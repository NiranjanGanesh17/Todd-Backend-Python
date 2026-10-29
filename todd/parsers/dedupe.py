"""Fingerprints. The same row arriving twice must have no effect.

§8.10 — never put free-text narration in an idempotency key. The model
transcribed a 32-character opaque UPI hash one character differently between
two runs, the fingerprint changed, and a re-parse silently duplicated every
affected transaction. Key on a *reference* instead.

Measured on 153 real rows: 27 arrived with a `ref_no`, 120 more were recovered
from a UTR inside the narration, 6 fell back to text — 96% keyed on a hard
reference, up from 18%.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import re

#: A UPI Unique Transaction Reference: exactly 12 digits, not part of a longer
#: number. The negative lookarounds matter — without them a 16-digit card
#: number yields a false "UTR".
UTR = re.compile(r"(?<![0-9])([0-9]{12})(?![0-9])")

#: Opaque machine ids inside narration: long alphanumeric runs that differ
#: between transcriptions of the same row. Dropped before text fallback.
OPAQUE = re.compile(r"[a-z0-9]{16,}")

#: Anything that is not a letter or a digit, for normalising a reference.
NON_ALNUM = re.compile(r"[^a-zA-Z0-9]+")

MIN_REFERENCE_LENGTH = 6


def identity_key(description: str, ref_no: str | None = None) -> str:
    """A stable identity for one transaction, independent of transcription.

    Prefers, in order: the reference the bank printed, a UTR found inside the
    narration, and only then the narration itself with opaque ids stripped.
    """
    reference = NON_ALNUM.sub("", ref_no or "").lower()
    if len(reference) >= MIN_REFERENCE_LENGTH:
        return f"ref:{reference}"

    utr = UTR.search(description or "")
    if utr:
        return f"ref:{utr.group(1)}"

    narration = OPAQUE.sub(" ", (description or "").lower())
    return f"txt:{' '.join(narration.split())}"


def fingerprint(
    *,
    account_id: str,
    txn_date: dt.date,
    amount_paise: int,
    direction: str,
    description: str,
    ref_no: str | None = None,
) -> str:
    """The value behind the `(user_id, fingerprint)` unique index.

    The account and the date are in the key because the same reference can
    legitimately recur across accounts, and because a bank occasionally reuses
    a short reference between months.
    """
    parts = (
        account_id,
        txn_date.isoformat(),
        str(amount_paise),
        direction,
        identity_key(description, ref_no),
    )
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()
