"""The Gmail sender allowlist (§8.14, §12).

This list is the privacy boundary: mail from anywhere else is never fetched.
"""

from __future__ import annotations

import pytest
from todd.parsers.senders import (
    ALLOWED_DOMAINS,
    SENDER_RULES,
    build_gmail_query,
    classify_message,
    institution_for_sender,
    is_allowed_sender,
)
from todd.schemas.enums import Institution, RawMessageKind


def test_every_bank_has_a_bank_in_domain() -> None:
    """§8.14 — the first backfill ran with only .com/.net and found 12
    messages, all marketing, 0 statements. With .bank.in: 142 messages,
    19 statements, 20 PDFs."""
    for rule in SENDER_RULES:
        assert any(d.endswith(".bank.in") for d in rule.domains), (
            f"{rule.institution.value} has no .bank.in domain"
        )


def test_the_older_domains_are_kept_too() -> None:
    """They still carry alerts for some customers; dropping them loses those."""
    for rule in SENDER_RULES:
        assert any(not d.endswith(".bank.in") for d in rule.domains)


def test_every_institution_except_unknown_is_covered() -> None:
    covered = {rule.institution for rule in SENDER_RULES}
    assert covered == set(Institution) - {Institution.UNKNOWN}


@pytest.mark.parametrize(
    ("address", "institution"),
    [
        ("estatement@icici.bank.in", Institution.ICICI),
        ("credit_cards@icici.bank.in", Institution.ICICI),
        ("alerts@hdfcbank.net", Institution.HDFC),
        ("noreply@bankofbaroda.bank.in", Institution.BOB),
        ("x@mail.sbi.co.in", Institution.SBI),  # subdomain
        ("Statements <estatement@axis.bank.in>", Institution.AXIS),  # display name
        ("ESTATEMENT@ICICI.BANK.IN", Institution.ICICI),  # case
    ],
)
def test_known_senders_resolve(address: str, institution: Institution) -> None:
    assert institution_for_sender(address) is institution


@pytest.mark.parametrize(
    "address",
    [
        "friend@gmail.com",
        "noreply@notabank.com",
        "phish@icici.bank.in.evil.com",  # suffix attack
        "icici.bank.in@evil.com",  # domain in the local part
        "",
    ],
)
def test_unknown_senders_are_never_fetched(address: str) -> None:
    assert institution_for_sender(address) is None
    assert not is_allowed_sender(address)


def test_the_query_only_asks_for_allowlisted_senders() -> None:
    query = build_gmail_query(6)
    assert query.count("from:") == len(ALLOWED_DOMAINS)
    assert "newer_than:6m" in query
    for domain in ALLOWED_DOMAINS:
        assert f"from:{domain}" in query


def test_a_statement_subject_is_classified_as_one() -> None:
    result = classify_message(
        from_address="estatement@icici.bank.in",
        subject="Statement of Transactions for your account",
        has_pdf_attachment=True,
    )
    assert result is not None
    assert result.kind is RawMessageKind.STATEMENT
    assert result.confident


def test_marketing_is_ignored_even_with_a_pdf_attached() -> None:
    """A pre-approved loan offer carries a PDF too. Ignore must win."""
    result = classify_message(
        from_address="estatement@icici.bank.in",
        subject="Congratulations! Your pre-approved loan offer",
        has_pdf_attachment=True,
    )
    assert result is not None
    assert result.kind is RawMessageKind.IGNORE


def test_an_alert_is_classified_as_an_alert() -> None:
    result = classify_message(
        from_address="alerts@hdfcbank.net",
        subject="You have done a UPI txn of Rs.250",
        has_pdf_attachment=False,
    )
    assert result is not None
    assert result.kind is RawMessageKind.TXN_ALERT


def test_an_unknown_sender_is_never_classified() -> None:
    assert (
        classify_message(
            from_address="friend@gmail.com",
            subject="Account statement",
            has_pdf_attachment=True,
        )
        is None
    )


def test_an_undecidable_subject_returns_none_rather_than_guessing() -> None:
    assert (
        classify_message(
            from_address="estatement@icici.bank.in",
            subject="Important information",
            has_pdf_attachment=False,
        )
        is None
    )
