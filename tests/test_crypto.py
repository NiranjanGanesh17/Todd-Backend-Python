"""Envelope encryption (§10, §12)."""

from __future__ import annotations

import pytest
from todd.config import DEV_MASTER_KEY
from todd.worker.lib.crypto import (
    Sealed,
    SealedSecretUnreadableError,
    generate_master_key,
    seal,
    try_unseal,
    unseal,
)

PASSWORD = "NIRA0101"


def test_a_sealed_secret_round_trips() -> None:
    key = generate_master_key()
    sealed = seal(PASSWORD, master_key=key, key_id="dev-1")
    assert unseal(sealed, master_key=key) == PASSWORD


def test_the_ciphertext_does_not_contain_the_plaintext() -> None:
    sealed = seal(PASSWORD, master_key=generate_master_key(), key_id="dev-1")
    assert PASSWORD not in sealed.ciphertext


def test_sealing_twice_gives_different_ciphertext() -> None:
    """A fresh data key and nonce per secret, so identical passwords across
    two users are not visibly identical in the table."""
    key = generate_master_key()
    assert (
        seal(PASSWORD, master_key=key, key_id="k").ciphertext
        != seal(PASSWORD, master_key=key, key_id="k").ciphertext
    )


def test_a_rotated_key_degrades_rather_than_crashing() -> None:
    """The password ladder tries every saved secret; one unreadable row must
    not abort the parse."""
    sealed = seal(PASSWORD, master_key=generate_master_key(), key_id="old")
    assert try_unseal(sealed, master_key=generate_master_key()) is None


def test_unseal_is_explicit_about_failure() -> None:
    sealed = seal(PASSWORD, master_key=generate_master_key(), key_id="old")
    with pytest.raises(SealedSecretUnreadableError):
        unseal(sealed, master_key=generate_master_key())


def test_corrupt_ciphertext_does_not_crash_the_ladder() -> None:
    assert try_unseal(Sealed("not-base64!!", "x", "k"), master_key=generate_master_key()) is None


def test_a_master_key_of_the_wrong_length_is_refused() -> None:
    with pytest.raises(ValueError, match="32 bytes"):
        seal(PASSWORD, master_key="c2hvcnQ=", key_id="k")


def test_the_development_master_key_actually_works() -> None:
    """The fallback key every developer runs with must be usable.

    Building a fresh key in each test left this one untested, and a constant
    that decoded to 35 bytes instead of 32 reached a live OAuth callback before
    anything noticed. Exercise the real value, not a stand-in.
    """
    sealed = seal(PASSWORD, master_key=DEV_MASTER_KEY, key_id="dev-1")
    assert unseal(sealed, master_key=DEV_MASTER_KEY) == PASSWORD


def test_the_default_settings_key_round_trips() -> None:
    """Whatever Settings hands the app, sealing with it must work."""
    from todd.config import Settings

    key = Settings(_env_file=None).encryption_master_key  # type: ignore[call-arg]
    assert key
    sealed = seal(PASSWORD, master_key=key, key_id="dev-1")
    assert unseal(sealed, master_key=key) == PASSWORD
