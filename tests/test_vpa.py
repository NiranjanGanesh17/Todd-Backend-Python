"""UPI address extraction and business-vs-person classification (§9 rung 6-7)."""

from __future__ import annotations

import pytest
from todd.parsers.vpa import Counterparty, classify_vpa, display_name_for, extract_vpa


@pytest.mark.parametrize(
    ("narration", "address"),
    [
        ("UPI/NEVATHA/itsnevatha@okaxis/Rent/ICICI", "itsnevatha@okaxis"),
        ("UPI-SWIGGY-q172501657@ybl-PAYMENT", "swiggy-q172501657@ybl"),
        ("NEFT-priya.sharma@oksbi-SALARY", "priya.sharma@oksbi"),
        ("UPI/vyapar.912345@hdfcbank/Shop", "vyapar.912345@hdfcbank"),
    ],
)
def test_an_address_is_found_in_real_narration(narration: str, address: str) -> None:
    found = extract_vpa(narration)
    assert found is not None
    assert found.address == address


def test_a_hyphenated_handle_survives_the_narration_split() -> None:
    """A hyphen separates fields in some banks' narration and is legal inside
    a handle, so the longest non-keyword candidate wins."""
    found = extract_vpa("UPI-ram-kumar@okicici-412345678901")
    assert found is not None
    assert found.address == "ram-kumar@okicici"


@pytest.mark.parametrize(
    "narration",
    ["POS 4035 PURCHASE", "please mail someone@example.com", "ATM CASH WITHDRAWAL", ""],
)
def test_text_without_a_upi_address_yields_none(narration: str) -> None:
    assert extract_vpa(narration) is None


def test_an_email_address_is_never_mistaken_for_a_vpa() -> None:
    """A dot-TLD cannot be a PSP handle."""
    assert extract_vpa("contact support@todd.app today") is None


@pytest.mark.parametrize(
    "address",
    [
        "q172501657@ybl",  # PhonePe merchant QR
        "paytmqr71d4q3@",  # Paytm QR, suffix clipped off entirely by ICICI
        "vyapar.912345@hdfcbank",
        "store.rzp@icici",
        "bflautopay@hdf",  # a mandate, never a person
        "paytm.s1j6uy8@pty",
    ],
)
def test_machine_issued_handles_are_businesses(address: str) -> None:
    assert classify_vpa(address) is Counterparty.BUSINESS


@pytest.mark.parametrize(
    "address",
    ["itsnevatha@okaxis", "ram.kumar@ybl", "john.doe@oksbi", "someone@okhdfcbank"],
)
def test_ordinary_handles_are_people(address: str) -> None:
    assert classify_vpa(address) is Counterparty.PERSON


def test_phonepe_serves_both_and_the_handle_decides() -> None:
    """`@ybl` is PhonePe for individuals *and* for merchant QRs, so keying on
    the suffix mislabels one of them."""
    assert classify_vpa("q172501657@ybl") is Counterparty.BUSINESS
    assert classify_vpa("ram.kumar@ybl") is Counterparty.PERSON


def test_a_truncated_suffix_still_resolves() -> None:
    """ICICI clips narration mid-address; `@hdf` is still HDFC."""
    assert classify_vpa("someone@hdf") is Counterparty.PERSON
    assert classify_vpa("someone@okax") is Counterparty.PERSON


def test_a_single_character_suffix_is_too_ambiguous_to_use() -> None:
    """`@p` prefixes paytm, pty and ptys alike."""
    assert classify_vpa("xbt3s@p") is Counterparty.UNKNOWN


def test_an_unknown_suffix_is_unknown_rather_than_guessed() -> None:
    assert classify_vpa("someone@notarealpsp") is Counterparty.UNKNOWN


@pytest.mark.parametrize(
    ("address", "name"),
    [
        ("john.doe@oksbi", "John Doe"),
        ("ram-kumar@okicici", "Ram Kumar"),
        ("itsnevatha@okaxis", "Itsnevatha"),
    ],
)
def test_a_person_gets_a_readable_name(address: str, name: str) -> None:
    assert display_name_for(address) == name


@pytest.mark.parametrize("address", ["xx1611@ybl", "e@pty", "xbt3s@ybl", "9630@okaxis"])
def test_a_machine_fragment_gets_no_name_at_all(address: str) -> None:
    """Title-casing `xx1611` produces a label that looks like data and means
    nothing — worse than a blank, because it invites trust."""
    assert display_name_for(address) is None


def test_a_merchant_never_gets_an_invented_name() -> None:
    assert display_name_for("q172501657@ybl") is None
