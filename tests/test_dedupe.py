"""Fingerprint stability (§8.10, §10)."""

from __future__ import annotations

import datetime as dt

from todd.parsers.dedupe import fingerprint, identity_key


def _fp(description: str, ref: str | None = None) -> str:
    return fingerprint(
        account_id="acc-1",
        txn_date=dt.date(2026, 4, 1),
        amount_paise=10_000,
        direction="debit",
        description=description,
        ref_no=ref,
    )


def test_a_one_character_hash_misread_does_not_duplicate_the_row() -> None:
    """The exact bug: the model transcribed a 32-character opaque UPI hash one
    character differently between runs, the fingerprint changed, and a re-parse
    silently duplicated every affected transaction."""
    first = "UPI/NEVATHA/a1b2c3d4e5f6g7h8i9j0k1l2/Rent/ICICI 412345678901"
    second = "UPI/NEVATHA/a1b2c3d4e5f6g7h8i9j0k1l3/Rent/ICICI 412345678901"
    assert identity_key(first) == identity_key(second) == "ref:412345678901"
    assert _fp(first) == _fp(second)


def test_a_printed_reference_wins_over_narration() -> None:
    assert identity_key("anything at all", "TXN-0098-7766") == "ref:txn00987766"


def test_a_utr_is_recovered_from_inside_the_narration() -> None:
    assert identity_key("NEFT CMS 123456789012 SALARY") == "ref:123456789012"


def test_a_sixteen_digit_card_number_is_not_a_utr() -> None:
    """Without the lookarounds a card number yields a false reference and two
    unrelated rows collapse into one."""
    assert not identity_key("CARD 4035123412341234").startswith("ref:")


def test_a_short_reference_is_not_trusted() -> None:
    """Fewer than six characters is not a reference, it is a column header."""
    assert identity_key("ATM WITHDRAWAL", "12").startswith("txt:")


def test_text_fallback_strips_opaque_ids_and_normalises_space() -> None:
    assert identity_key("UPI/abcdefghijklmnopqrst/Rent") == "txt:upi/ /rent"
    assert identity_key("  ATM   CASH  ") == "txt:atm cash"


def test_different_amounts_are_different_transactions() -> None:
    left = fingerprint(
        account_id="a",
        txn_date=dt.date(2026, 4, 1),
        amount_paise=100,
        direction="debit",
        description="same",
    )
    right = fingerprint(
        account_id="a",
        txn_date=dt.date(2026, 4, 1),
        amount_paise=200,
        direction="debit",
        description="same",
    )
    assert left != right


def test_the_same_reference_on_two_accounts_stays_distinct() -> None:
    args = {
        "txn_date": dt.date(2026, 4, 1),
        "amount_paise": 100,
        "direction": "debit",
        "description": "x",
        "ref_no": "REF123456",
    }
    assert fingerprint(account_id="a", **args) != fingerprint(account_id="b", **args)  # type: ignore[arg-type]
