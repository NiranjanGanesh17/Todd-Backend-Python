"""Envelope encryption: a per-secret data key, sealed by the master key.

Two layers so a master-key rotation does not require re-encrypting every
secret, and so one leaked data key exposes one secret rather than all of them.

A secret sealed under a key that no longer exists must *degrade*, not crash:
the password ladder tries every saved secret, and one unreadable row cannot be
allowed to abort the parse.
"""

from __future__ import annotations

from dataclasses import dataclass

import nacl.secret
import nacl.utils
from nacl.encoding import Base64Encoder
from nacl.exceptions import CryptoError

from todd.log import get_logger

logger = get_logger(__name__)

KEY_BYTES = nacl.secret.SecretBox.KEY_SIZE


class SealedSecretUnreadableError(Exception):
    """The ciphertext cannot be opened with the key offered."""


@dataclass(frozen=True, slots=True)
class Sealed:
    ciphertext: str
    nonce: str
    key_id: str


def _master_box(master_key: str) -> nacl.secret.SecretBox:
    raw = Base64Encoder.decode(master_key.encode("ascii"))
    if len(raw) != KEY_BYTES:
        raise ValueError(f"ENCRYPTION_MASTER_KEY must decode to {KEY_BYTES} bytes, got {len(raw)}")
    return nacl.secret.SecretBox(raw)


def generate_master_key() -> str:
    """A fresh master key, base64. For provisioning, never at runtime (§8.16)."""
    return Base64Encoder.encode(nacl.utils.random(KEY_BYTES)).decode("ascii")


def seal(plaintext: str, *, master_key: str, key_id: str) -> Sealed:
    """Encrypt one secret under a fresh data key, itself sealed by the master.

    The stored ciphertext is `sealed_data_key || box(plaintext, data_key)`.
    """
    data_key = nacl.utils.random(KEY_BYTES)
    nonce = nacl.utils.random(nacl.secret.SecretBox.NONCE_SIZE)

    sealed_data_key = _master_box(master_key).encrypt(data_key)
    body = nacl.secret.SecretBox(data_key).encrypt(plaintext.encode("utf-8"), nonce)

    return Sealed(
        ciphertext=Base64Encoder.encode(bytes(sealed_data_key) + bytes(body)).decode("ascii"),
        nonce=Base64Encoder.encode(nonce).decode("ascii"),
        key_id=key_id,
    )


def unseal(sealed: Sealed, *, master_key: str) -> str:
    """Decrypt, or raise `SealedSecretUnreadableError`.

    Callers walking the password ladder should catch this and move on with a
    warning: a secret written under a rotated key is expected, not exceptional.
    """
    try:
        blob = Base64Encoder.decode(sealed.ciphertext.encode("ascii"))
        # A sealed 32-byte key is 32 + 24 (nonce) + 16 (MAC) = 72 bytes.
        sealed_key_len = KEY_BYTES + nacl.secret.SecretBox.NONCE_SIZE + 16
        sealed_data_key, body = blob[:sealed_key_len], blob[sealed_key_len:]

        data_key = _master_box(master_key).decrypt(sealed_data_key)
        return nacl.secret.SecretBox(data_key).decrypt(body).decode("utf-8")
    except (CryptoError, ValueError, IndexError) as exc:
        raise SealedSecretUnreadableError(
            f"secret sealed under key_id={sealed.key_id} could not be opened"
        ) from exc


def try_unseal(sealed: Sealed, *, master_key: str) -> str | None:
    """`unseal`, degraded to None with a warning. For the password ladder."""
    try:
        return unseal(sealed, master_key=master_key)
    except SealedSecretUnreadableError:
        logger.warning("sealed_secret_unreadable", key_id=sealed.key_id)
        return None
