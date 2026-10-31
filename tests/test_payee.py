"""Payee-name extraction (§9) and the redaction that guards rung 8 (§8.6, §12)."""

from __future__ import annotations

import pytest
from todd.parsers.llm.pricing import cost_of, rates_for
from todd.parsers.llm.redact import redact, redact_payee
from todd.parsers.payee import extract_payee, normalise_payee
from todd.worker.agents.enrich import _looks_personal


@pytest.mark.parametrize(
    ("narration", "payee"),
    [
        ("UPI/SWIGGY/bundl technologies", "SWIGGY"),
        ("BIGTREE ENTERTAINMENT PRIVATE LIMITED UPI", "BIGTREE ENTERTAINMENT"),
        ("ALADDIN SHAWARMA UPI", "ALADDIN SHAWARMA"),
        ("L/645874/ICIf9d8/ UPI/BAJAJ FINA/bflautopay@hdf", "BAJAJ FINA"),
    ],
)
def test_the_printed_name_is_found(narration: str, payee: str) -> None:
    assert extract_payee(narration) == payee


def test_a_truncated_repeat_is_dropped() -> None:
    """Banks print the payee twice and cut the second copy short."""
    assert (
        extract_payee("ICIe9e7 BISMILLAH M A CHICKEN AND MUTTON CENTER UPI/BISMILLAH/x@")
        == "BISMILLAH CHICKEN AND MUTTON CENTER"
    )


@pytest.mark.parametrize(
    "narration",
    [
        "UPI/130465770775/13:52:39/UPI/paytm.s1auls1",
        "ACH/HDFC BANK",
        "",
        "L/610213183067/ICI910d7cac8e4e4db7b993037cabb1ee70",
    ],
)
def test_machine_strings_yield_no_payee(narration: str) -> None:
    """Rail names and hashes are uppercase too; a payee of "UPI" or
    "HDFC BANK" would be worse than none."""
    assert extract_payee(narration) is None


def test_the_cache_key_is_stable() -> None:
    assert normalise_payee("BAJAJ FINANCE LTD.") == "bajaj finance ltd"
    assert normalise_payee("  Swiggy  ") == "swiggy"
    assert normalise_payee(None) is None


# ------------------------------------------------- the privacy gate (§12)


@pytest.mark.parametrize("payee", ["ganesan", "asif pasha", "ramesh kumar"])
def test_an_individual_is_never_sent_to_the_model(payee: str) -> None:
    """§12 forbids sending names, and a model cannot categorise a stranger."""
    assert _looks_personal(payee)


@pytest.mark.parametrize(
    "payee",
    [
        "bismillah chicken and mutton center",
        "bajaj finance",
        "aladdin shawarma",
        "bigtree entertainment",
        "balaji tiffins",
    ],
)
def test_a_business_is_sendable(payee: str) -> None:
    assert not _looks_personal(payee)


def test_a_business_upi_handle_overrides_the_name_heuristic() -> None:
    """Rung 6 decided this from a merchant QR, which cannot be faked."""
    assert _looks_personal("some name")
    assert not _looks_personal("some name", vpa_is_business=True)


# ------------------------------------------------------- redaction (§8.6)


def test_a_twelve_digit_upi_reference_survives_redaction() -> None:
    """§8.6 — eating the UTR destroys the key deduplication relies on (§8.10),
    which silently duplicates transactions on a re-parse."""
    out = redact("UPI/412345678901/PAYMENT").text
    assert "412345678901" in out


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("PAN ABCDE1234F", "pan"),
        ("call 9876543210", "phone"),
        ("mail a@b.com", "email"),
        ("1234 5678 9012", "aadhaar"),
        ("4035123412341234", "card"),
        ("acct 123456789012345", "account"),
    ],
)
def test_pii_is_removed(text: str, kind: str) -> None:
    result = redact(text)
    assert kind in result.counts
    assert result.counts[kind] >= 1


def test_redaction_reports_what_it_removed() -> None:
    """Counts are returned so the removal is visible, not assumed."""
    assert redact("ABCDE1234F and 9876543210").counts == {"pan": 1, "phone": 1}


def test_a_merchant_name_passes_through_clean() -> None:
    assert redact_payee("BISMILLAH CHICKEN AND MUTTON CENTER") == (
        "BISMILLAH CHICKEN AND MUTTON CENTER"
    )


# --------------------------------------------------------------- pricing


def test_cost_is_integer_micros() -> None:
    cost = cost_of(model="claude-haiku-5-5", input_tokens=2000, output_tokens=2500)
    assert isinstance(cost.micros, int)
    assert cost.micros == 1450  # 2000*0.10/1M + 2500*0.50/1M dollars, in micros


def test_a_long_haiku_prompt_costs_more() -> None:
    assert rates_for("claude-haiku-5-5", input_tokens=10) == (0.10, 0.50)
    assert rates_for("claude-haiku-5-5", input_tokens=200_000) == (0.50, 2.50)


def test_an_unpriced_model_raises_rather_than_guessing() -> None:
    with pytest.raises(ValueError, match="no published rate"):
        rates_for("claude-not-a-model")
