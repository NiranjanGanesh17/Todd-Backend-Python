"""Analysis-window arithmetic (§8.15)."""

from __future__ import annotations

import datetime as dt

import pytest
from todd.parsers.dates import (
    gap_label,
    last_complete_months,
    month_bounds,
    month_key,
    preceding_months,
    window_label,
)


def test_a_window_ends_last_month_not_this_one() -> None:
    """Statements arrive in arrears. A window including the current month
    opens on an almost empty report."""
    window = last_complete_months(6, today=dt.date(2026, 10, 29))
    assert window[-1] == "2026-09"
    assert "2026-10" not in window
    assert window == ["2026-04", "2026-05", "2026-06", "2026-07", "2026-08", "2026-09"]


def test_a_window_crosses_a_year_boundary() -> None:
    assert last_complete_months(3, today=dt.date(2026, 2, 1)) == ["2025-11", "2025-12", "2026-01"]


@pytest.mark.parametrize(
    ("keys", "expected"),
    [
        (["2026-04", "2026-09"], "Apr – Sep"),
        (["2025-10", "2026-09"], "Oct 2025 – Sep 2026"),
        (["2025-12"], "Dec 2025"),
    ],
)
def test_window_label_is_written_by_the_server(keys: list[str], expected: str) -> None:
    assert window_label(keys) == expected


def test_month_bounds_and_key_round_trip() -> None:
    start, end = month_bounds("2026-02")
    assert (start, end) == (dt.date(2026, 2, 1), dt.date(2026, 2, 28))
    assert month_key(start) == "2026-02"


def test_a_window_must_have_at_least_one_month() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        last_complete_months(0)


@pytest.mark.parametrize(
    ("keys", "expected"),
    [
        ([], ""),
        (["2026-07"], "Jul"),
        (["2026-07", "2026-08", "2026-09"], "Jul – Sep"),
        (["2026-09", "2026-07", "2026-08"], "Jul – Sep"),
        (["2026-04", "2026-06", "2026-09"], "3 months"),
        (["2025-12", "2026-01"], "Dec – Jan"),
    ],
)
def test_gap_label_says_a_range_or_a_count(keys: list[str], expected: str) -> None:
    """Profile prints "Missing Jul – Sep"; scattered months are counted instead."""
    assert gap_label(keys) == expected


def test_the_comparison_window_is_the_one_immediately_before() -> None:
    """A trend is measured against an equally long window, not last year."""
    assert preceding_months(["2026-07", "2026-08", "2026-09"]) == [
        "2026-04",
        "2026-05",
        "2026-06",
    ]


def test_a_comparison_window_crosses_the_year_end() -> None:
    assert preceding_months(["2026-01", "2026-02"]) == ["2025-11", "2025-12"]
