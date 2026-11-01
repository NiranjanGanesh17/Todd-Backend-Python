"""How far into a PDF the parser has got, while it is still parsing.

The upload screen shows "Page 3 of 6" against a file. That number cannot come
from the database: a parse is one transaction, and a counter written inside it
is invisible to the request asking about it until the whole document is done —
by which time nobody needs it.

So it lives in memory, in the process doing the parsing. Upload and parsing
share a process today (§13 — there is no queue yet), so the status endpoint
can read it. When that stops being true this module is the thing to replace,
and the screen degrades to "Reading…" rather than lying: a document nobody
here is parsing simply has no entry.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

__all__ = ["Progress", "advance", "clear", "read", "start"]


@dataclass(frozen=True, slots=True)
class Progress:
    page: int
    page_count: int


#: Parsing is synchronous inside one task, but several uploads can be in
#: flight, so the board is guarded.
_LOCK = threading.Lock()
_BOARD: dict[str, Progress] = {}


def start(document_id: str, page_count: int) -> None:
    with _LOCK:
        _BOARD[document_id] = Progress(page=0, page_count=page_count)


def advance(document_id: str, page: int, page_count: int) -> None:
    with _LOCK:
        _BOARD[document_id] = Progress(page=page, page_count=page_count)


def clear(document_id: str) -> None:
    with _LOCK:
        _BOARD.pop(document_id, None)


def read(document_id: str) -> Progress | None:
    with _LOCK:
        return _BOARD.get(document_id)
