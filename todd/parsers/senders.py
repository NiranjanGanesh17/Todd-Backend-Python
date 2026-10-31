"""The Gmail sender allowlist.

§8.14 — Indian banks have moved to the RBI-mandated `.bank.in` domains, and
that is where statements now come from: `estatement@icici.bank.in`,
`credit_cards@icici.bank.in`. The original allowlist held only `.com`/`.net`,
and the first backfill found 12 messages, all marketing, 0 statements. After
adding `.bank.in`: 142 messages, 19 statements, 20 PDFs.

Both are listed. The older domains still carry alerts for some customers, and
dropping them loses those.

The Gmail search query is built from this list, so mail from any other sender
is never fetched and never stored. That is the privacy boundary: a wrong entry
means silently missing statements, a too-broad one means reading mail we have
no business reading.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from todd.schemas.enums import Institution, RawMessageKind

#: Subjects that mean "not a statement and not an alert" for every bank.
COMMON_IGNORE: tuple[str, ...] = (
    "otp",
    "one time password",
    "offer",
    "offers",
    "congratulations",
    "pre-approved",
    "pre approved",
    "loan offer",
    "insurance offer",
    "newsletter",
    "survey",
    "feedback",
    "win ",
    "cashback offer",
)


@dataclass(frozen=True, slots=True)
class SenderRule:
    institution: Institution
    #: Matched against the lowercased address by domain suffix.
    domains: tuple[str, ...]
    statement_subjects: tuple[str, ...]
    alert_subjects: tuple[str, ...]
    ignore_subjects: tuple[str, ...] = field(default=COMMON_IGNORE)


SENDER_RULES: tuple[SenderRule, ...] = (
    SenderRule(
        institution=Institution.HDFC,
        domains=("hdfc.bank.in", "hdfcbank.net", "hdfcbank.com"),
        statement_subjects=(
            "account statement",
            "statement of account",
            "credit card statement",
            "e-statement",
            "estatement",
        ),
        alert_subjects=(
            "you have done a upi txn",
            "debit card transaction",
            "transaction alert",
            "has been debited",
            "has been credited",
        ),
    ),
    SenderRule(
        institution=Institution.ICICI,
        domains=("icici.bank.in", "icicibank.com", "icicibank.net", "icicibankalerts.com"),
        statement_subjects=(
            "statement of transactions",
            "account statement",
            "credit card statement",
            "e-statement",
        ),
        alert_subjects=(
            "transaction alert",
            "debited",
            "credited",
            "upi transaction",
            "txn alert",
        ),
    ),
    SenderRule(
        institution=Institution.SBI,
        domains=("sbi.bank.in", "sbi.co.in", "alerts.sbi.co.in", "onlinesbi.sbi", "sbicard.com"),
        statement_subjects=(
            "account statement",
            "e-statement",
            "monthly statement",
            "credit card statement",
        ),
        alert_subjects=("transaction alert", "debited", "credited", "upi txn", "txn alert"),
    ),
    SenderRule(
        institution=Institution.AXIS,
        domains=("axis.bank.in", "axisbank.com", "axisbank.co.in", "alerts.axisbank.com"),
        statement_subjects=(
            "account statement",
            "statement of account",
            "credit card statement",
            "e-statement",
        ),
        alert_subjects=("transaction alert", "debited", "credited", "upi transaction"),
    ),
    SenderRule(
        institution=Institution.KOTAK,
        domains=("kotak.bank.in", "kotak.com", "kotakbank.com", "alerts.kotak.com"),
        statement_subjects=(
            "account statement",
            "statement of account",
            "credit card statement",
            "e-statement",
        ),
        alert_subjects=("transaction alert", "debited", "credited", "upi transaction"),
    ),
    SenderRule(
        institution=Institution.BOB,
        domains=("bankofbaroda.bank.in", "bankofbaroda.com", "bankofbaroda.co.in"),
        statement_subjects=(
            "account statement",
            "e-statement",
            "estatement",
            "statement of account",
        ),
        alert_subjects=("transaction alert", "debited", "credited", "upi transaction"),
    ),
)

ALLOWED_DOMAINS: tuple[str, ...] = tuple(
    dict.fromkeys(domain for rule in SENDER_RULES for domain in rule.domains)
)


def build_gmail_query(window_months: int) -> str:
    """The single Gmail search the inbox agent runs. Nothing outside it is
    ever fetched."""
    senders = " OR ".join(f"from:{domain}" for domain in ALLOWED_DOMAINS)
    return f"({senders}) newer_than:{window_months}m"


def _domain_of(address: str) -> str:
    lowered = address.strip().lower()
    # "Name <a@b.com>" as well as a bare address.
    if "<" in lowered and ">" in lowered:
        lowered = lowered[lowered.rindex("<") + 1 : lowered.rindex(">")]
    return lowered.rsplit("@", 1)[-1].strip()


def institution_for_sender(from_address: str) -> Institution | None:
    domain = _domain_of(from_address)
    for rule in SENDER_RULES:
        if any(domain == d or domain.endswith(f".{d}") for d in rule.domains):
            return rule.institution
    return None


def is_allowed_sender(from_address: str) -> bool:
    return institution_for_sender(from_address) is not None


@dataclass(frozen=True, slots=True)
class Classification:
    kind: RawMessageKind
    institution: Institution
    #: False when the decision rested on an attachment rather than the subject.
    confident: bool


def classify_message(
    *, from_address: str, subject: str, has_pdf_attachment: bool
) -> Classification | None:
    """Classify from sender, subject and whether a PDF rode along.

    Exact and free. `None` means the rules could not decide, which is the
    caller's cue to leave the message unclassified rather than guess.
    """
    institution = institution_for_sender(from_address)
    if institution is None:
        return None

    rule = next(r for r in SENDER_RULES if r.institution is institution)
    lowered = (subject or "").lower()

    if any(fragment in lowered for fragment in rule.ignore_subjects):
        return Classification(RawMessageKind.IGNORE, institution, confident=True)

    looks_statement = any(fragment in lowered for fragment in rule.statement_subjects)
    if looks_statement or has_pdf_attachment:
        return Classification(RawMessageKind.STATEMENT, institution, confident=looks_statement)

    if any(fragment in lowered for fragment in rule.alert_subjects):
        return Classification(RawMessageKind.TXN_ALERT, institution, confident=True)

    return None
