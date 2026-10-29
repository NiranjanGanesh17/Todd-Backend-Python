"""Money parsing. Every case here is a bug that shipped (§8.2–8.4, §10)."""

from __future__ import annotations

import pytest
from todd.parsers.money import (
    CREDIT_MARKER,
    format_paise,
    looks_like_amount,
    parse_indian_amount,
)


@pytest.mark.parametrize(
    ("text", "paise"),
    [
        ("1,23,456.78", 12_345_678),  # Indian grouping, not 123,456.78
        ("1,234", 123_400),  # whole rupees
        ("0.01", 1),  # one paise survives
        ("0", 0),
        ("2,50,000.50", 25_000_050),
        ("1234.5", 123_450),  # one decimal digit means tenths
    ],
)
def test_indian_grouping(text: str, paise: int) -> None:
    amount = parse_indian_amount(text)
    assert amount is not None
    assert amount.paise == paise


@pytest.mark.parametrize("text", ["Rs.1,234", "Rs 1,234", "₹1,234", "INR1,234", "rs.1,234"])
def test_currency_marks_are_stripped_before_the_decimal_point(text: str) -> None:
    """§8.4 — "Rs.1,234" reduced to ".1,234" and parsed as a decimal gives
    12 paise. Strip currency first, then shed the stray dot."""
    amount = parse_indian_amount(text)
    assert amount is not None
    assert amount.paise == 123_400, f"{text} must be ₹1,234, not {amount.paise} paise"


@pytest.mark.parametrize(
    ("text", "marker"),
    [
        ("1,234.00Cr", "cr"),  # no word boundary between "0" and "C"
        ("1,234.00CR", "cr"),
        ("1,234.00 Cr", "cr"),
        ("500Dr", "dr"),
        ("500 DR", "dr"),
        ("1,234.00", None),
    ],
)
def test_dr_cr_markers_need_a_non_word_boundary_guard(text: str, marker: str | None) -> None:
    """§8.3 — `\\bCr\\b` cannot match in "1,234.00Cr"."""
    amount = parse_indian_amount(text)
    assert amount is not None
    assert amount.marker == marker


def test_the_credit_marker_does_not_fire_inside_a_longer_word() -> None:
    """ "CREDITCARD" and "SCRIP" must not read as a credit marker."""
    assert CREDIT_MARKER.search("1,234.00Cr")
    assert not CREDIT_MARKER.search("CREDITCARD PAYMENT")
    assert not CREDIT_MARKER.search("DESCRIPTION")


@pytest.mark.parametrize(
    ("text", "paise"),
    [("(1,234.00)", -123_400), ("-1,234.00", -123_400), ("(500)", -50_000)],
)
def test_a_parenthesised_amount_is_negative(text: str, paise: int) -> None:
    amount = parse_indian_amount(text)
    assert amount is not None
    assert amount.paise == paise


@pytest.mark.parametrize("text", ["", "   ", "abc", None, "4035XXXXXXXX1008", "UPI/NEVATHA", "--"])
def test_a_non_amount_is_none_not_zero(text: str | None) -> None:
    """None, never 0: a reference that fails to parse must not silently become
    a zero-rupee row, and 0 is itself a legitimate amount."""
    assert parse_indian_amount(text) is None
    assert not looks_like_amount(text)


def test_zero_is_an_amount_and_is_not_none() -> None:
    parsed = parse_indian_amount("0.00")
    assert parsed is not None and parsed.paise == 0


def test_money_never_becomes_a_float() -> None:
    for text in ("0.01", "1,23,456.78", "999999999.99"):
        parsed = parse_indian_amount(text)
        assert parsed is not None
        assert isinstance(parsed.paise, int)
        assert not isinstance(parsed.paise, bool)


def test_a_third_of_a_rupee_does_not_drift() -> None:
    """The reason paise are integers: 0.1 + 0.2 != 0.3 in binary floating point,
    and a statement must reconcile exactly."""
    total = sum(parse_indian_amount(t).paise for t in ("0.10", "0.20"))  # type: ignore[union-attr]
    assert total == 30


@pytest.mark.parametrize(
    ("paise", "rendered"),
    [
        (12_345_678, "₹1,23,456.78"),
        (100, "₹1.00"),
        (1, "₹0.01"),
        (0, "₹0.00"),
        (-123_456_789, "-₹12,34,567.89"),
        (100_000_000, "₹10,00,000.00"),
    ],
)
def test_rendering_uses_indian_grouping(paise: int, rendered: str) -> None:
    assert format_paise(paise) == rendered


def test_parse_and_render_round_trip() -> None:
    for text in ("1,23,456.78", "0.01", "99,999.99"):
        parsed = parse_indian_amount(text)
        assert parsed is not None
        assert format_paise(parsed.paise, symbol=False) == text
