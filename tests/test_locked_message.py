"""The sentence on Home's locked-statement banner.

A locked PDF is a sealed envelope — the bank, the account and the period are
all inside it. The banner used to be assembled on the client as
"Your {label} statements need a password", and for an upload with no readable
bank the label fell back to the word "locked", producing "Your locked
statements need a password": a sentence that names nothing.

These cases pin the wording to what is actually knowable about a sealed file.
"""

import datetime as dt

import pytest
from todd.api.routes.screens import _locked_message

TODAY = dt.date(2026, 11, 3)


@pytest.mark.parametrize(
    ("institution", "account_type", "pending", "added_on", "expected"),
    [
        # Nothing known: lean on when the user put the files there.
        (None, None, 3, TODAY, "3 statements you added today are password-protected"),
        (
            None,
            None,
            2,
            TODAY - dt.timedelta(days=1),
            "2 statements you added yesterday are password-protected",
        ),
        (
            None,
            None,
            2,
            dt.date(2026, 10, 28),
            "2 statements you added on 28 Oct are password-protected",
        ),
        # An explicit "unknown" institution is no more nameable than a null one.
        (
            "unknown",
            None,
            4,
            dt.date(2026, 10, 28),
            "4 statements you added on 28 Oct are password-protected",
        ),
        # Once there is a bank or an account, that is the better handle.
        ("icici", None, 2, dt.date(2026, 10, 28), "Your ICICI statements need a password"),
        ("icici", "credit_card", 1, None, "Your ICICI card statements need a password"),
    ],
)
def test_locked_message(institution, account_type, pending, added_on, expected):
    assert _locked_message(institution, account_type, pending, added_on, TODAY) == expected


def test_one_file_is_singular():
    """ "1 statements are" is the kind of sentence that makes an app look broken."""
    assert (
        _locked_message(None, None, 1, TODAY, TODAY)
        == "1 statement you added today is password-protected"
    )


def test_never_says_the_word_locked():
    """The bug this replaced: "locked" as the name of the thing."""
    for pending in (1, 2, 9):
        message = _locked_message(None, None, pending, TODAY, TODAY)
        assert "Your locked" not in message
        assert "locked statements" not in message
