"""Object storage: a local directory in development, Supabase in production.

Keys are `users/{user_id}/documents/{document_id}/{filename}`, so a tenant's
objects are a prefix — which is what makes a DPDP deletion request a prefix
delete rather than a scan.
"""

from __future__ import annotations

import hashlib
import re
import shutil
from pathlib import Path
from typing import Protocol

from todd.config import Settings

#: Filenames arrive from email attachments and browser uploads. Neither is
#: trusted: a name containing `../` must not escape the tenant's prefix.
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def safe_filename(name: str, *, fallback: str = "statement.pdf") -> str:
    cleaned = _UNSAFE.sub("_", Path(name).name).strip("._")
    return cleaned[:120] or fallback


def storage_key(user_id: str, document_id: str, filename: str) -> str:
    return f"users/{user_id}/documents/{document_id}/{safe_filename(filename)}"


def sha256_of(data: bytes) -> str:
    """The content hash behind the `(user_id, sha256)` unique index — the same
    PDF arriving twice is one document."""
    return hashlib.sha256(data).hexdigest()


class Storage(Protocol):
    def put(self, key: str, data: bytes) -> str: ...
    def get(self, key: str) -> bytes: ...
    def delete_prefix(self, prefix: str) -> int: ...
    def exists(self, key: str) -> bool: ...


class LocalStorage:
    """Development storage: a directory, gitignored."""

    def __init__(self, root: Path | str = ".todd-storage") -> None:
        self.root = Path(root)

    def _path(self, key: str) -> Path:
        target = (self.root / key).resolve()
        root = self.root.resolve()
        if not target.is_relative_to(root):
            raise ValueError(f"storage key escapes the root: {key!r}")
        return target

    def put(self, key: str, data: bytes) -> str:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return key

    def get(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def exists(self, key: str) -> bool:
        return self._path(key).exists()

    def delete_prefix(self, prefix: str) -> int:
        target = self._path(prefix)
        if not target.exists():
            return 0
        count = sum(1 for _ in target.rglob("*") if _.is_file())
        shutil.rmtree(target)
        return count


def build_storage(settings: Settings) -> Storage:
    """Local directory unless Supabase is configured.

    Supabase storage is not wired yet; this raises rather than silently writing
    a production statement to the worker's local disk.
    """
    if settings.is_production:
        raise NotImplementedError(
            "Supabase storage is not implemented. Statements must not be written "
            "to local disk in production."
        )
    return LocalStorage()
