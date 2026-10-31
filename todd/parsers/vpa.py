"""UPI addresses: pulling them out of narration, and telling business from person.

§9 rung 6 is free and high-yield. A UPI handle carries more signal than it
looks: `q172501657@ybl` is a PhonePe merchant QR, never an individual;
`vyapar.`, `paytm.` and `.rzp@` are business aggregators; a plain `name@oka`
is a person. On the real data that split 17 businesses from 96 individuals
with no model involved.

This is also the §7.4 rule in practice: the model is not a field-splitter.
Extract the UPI reference here, not in a prompt.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

#: A whole UPI address: `name@psp`. The PSP part may be **empty or truncated**:
#: ICICI clips narration mid-address, so `paytmqr71d4q3@` and `bflautopay@hdf`
#: both occur in real statements. Requiring a complete suffix loses them.
#: An email still cannot match, because a dot-TLD is not `[a-zA-Z]{0,15}`.
VPA = re.compile(r"^([a-zA-Z0-9](?:[a-zA-Z0-9._-]{0,48}[a-zA-Z0-9])?)@([a-zA-Z]{0,15})$")

#: Narration delimiters. A hyphen is deliberately NOT here: it separates fields
#: in some banks' narration *and* is legal inside a handle, so it is tried only
#: as a fallback split.
DELIMITERS = re.compile(r"[/\s|,;:*#]+")

# --------------------------------------------------------------------------
# Merchant signals live in the HANDLE, not the suffix.
#
# The suffix is a poor discriminator and truncation makes it worse: `@ybl` is
# PhonePe for individuals *and* for merchant QRs, and `@hdf` could be either.
# What a merchant account reliably carries is a machine-issued handle.
# --------------------------------------------------------------------------

#: Machine-issued merchant handles: a QR id, or an aggregator's prefix.
MERCHANT_HANDLE = re.compile(
    r"""^(?:
          q\d{6,}                      # PhonePe merchant QR: q172501657@ybl
        | paytmqr\w*                   # Paytm merchant QR
        | (?:vyapar|bharatpe|bajajpay|pine|merchant|gpay|razorpay|payu)\b.*
        | \w*\.(?:rzp|payu|cf|ipg|pz|gpay)\b.*   # gateway sub-handles
        | paytm\.\w+                  # paytm.s1j6uy8
        | \w*autopay\w*               # bflautopay — a mandate, never a person
        | \w*\.(?:ifsc|biz|store|shop)\b.*
      )$""",
    re.IGNORECASE | re.VERBOSE,
)

#: Consumer PSP suffixes, matched by **prefix** so a clipped `@ok`, `@apl` or
#: `@hdf` still resolves.
PERSON_PSPS = (
    "oksbi", "okaxis", "okhdfcbank", "okicici", "okbizaxis",
    "apl", "yapl", "axl", "ibl", "ybl", "upi",
    "paytm", "ptyes", "ptsbi", "ptaxis", "pty", "ptys",
    "waaxis", "wasbi", "wahdfcbank", "waicici",
    "hdfcbank", "icici", "axisbank", "sbi", "kotak", "idfcbank", "indus",
    "fam", "jupiteraxis", "naviaxis", "yesg", "airtel", "freecharge", "jio",
)  # fmt: skip


class Counterparty(StrEnum):
    BUSINESS = "business"
    PERSON = "person"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class Vpa:
    address: str
    handle: str
    psp: str

    @property
    def kind(self) -> Counterparty:
        return classify_vpa(self.address)


#: Narration keywords that precede the real handle. A handle never starts with
#: one, so a hyphen-split candidate beginning here is a prefix, not a payee.
NARRATION_WORDS = frozenset(
    {"upi", "neft", "imps", "rtgs", "pos", "ach", "atm", "tpt", "bil", "inf", "nach", "mmt"}
)


def _candidates(text: str) -> list[str]:
    """Narration split into things that might be a whole VPA.

    Unambiguous delimiters first. A token that still holds an `@` but is not
    itself an address is then split on hyphens — which a handle may legally
    contain, so the pieces are rejoined longest-first and any candidate
    starting with a narration keyword is skipped. That reads
    `UPI-ram-kumar@okicici-412…` as `ram-kumar@okicici` rather than losing the
    first half of the name, while still finding `q172501657@ybl` inside
    `UPI-SWIGGY-q172501657@ybl-PAYMENT`.
    """
    out: list[str] = []
    for token in (t for t in DELIMITERS.split(text) if "@" in t):
        out.append(token)
        if VPA.match(token) or "-" not in token:
            continue

        parts = token.split("-")
        at = next((i for i, part in enumerate(parts) if "@" in part), None)
        if at is None:
            continue
        # Longest first: "ram-kumar@okicici" before "kumar@okicici".
        for start in range(at + 1):
            if parts[start].lower() in NARRATION_WORDS:
                continue
            out.append("-".join(parts[start : at + 1]))
    return out


def extract_vpa(text: str | None) -> Vpa | None:
    """The first UPI address in the narration, or None.

    The first, not the longest: Indian narration puts the payee before the
    payer, so the first address is the counterparty.
    """
    if not text:
        return None
    for candidate in _candidates(text):
        match = VPA.match(candidate)
        if match is None:
            continue
        handle, psp = match.group(1), match.group(2)
        return Vpa(address=f"{handle}@{psp}".lower(), handle=handle.lower(), psp=psp.lower())
    return None


def classify_vpa(address: str | None) -> Counterparty:
    """Business, person, or unknown.

    The handle decides. Only if it looks like nothing in particular does the
    PSP suffix get a say, and then by prefix — a clipped `@hdf` still names
    HDFC, and `@ok` still names a Google Pay consumer handle.
    """
    if not address or "@" not in address:
        return Counterparty.UNKNOWN

    handle, _, psp = address.lower().partition("@")
    if not handle:
        return Counterparty.UNKNOWN

    if MERCHANT_HANDLE.match(handle):
        return Counterparty.BUSINESS

    # A truncated suffix is still evidence, as long as it is unambiguous. One
    # character is not: `@p` prefixes paytm, pty and ptys alike.
    if len(psp) >= 2 and any(known.startswith(psp) for known in PERSON_PSPS):
        return Counterparty.PERSON

    return Counterparty.UNKNOWN


def display_name_for(address: str | None) -> str | None:
    """A readable name for a person's VPA — `john.doe@oksbi` -> "John Doe".

    Returns None unless the handle genuinely reads as a name. Real handles are
    full of machine fragments (`xx1611`, `xbt3s`, `e`), and title-casing those
    produces a label that looks like data but means nothing — worse than an
    honest blank, because it invites the reader to trust it.
    """
    if not address or classify_vpa(address) is not Counterparty.PERSON:
        return None

    handle = address.partition("@")[0]
    words = [w for w in re.split(r"[._\-]+", handle) if w.isalpha() and len(w) >= 3]
    if not words:
        return None

    name = " ".join(words)
    if len(name) < 4:  # a single short fragment is a username, not a name
        return None
    return name.title()
