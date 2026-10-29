"""pdfplumber -> positioned words, plus `page.lines` and `page.rects`.

The Node version threw ruling lines away. The table extractor does not need
them today, but a credit-card statement with a boxed table is the case where
cropping to the ruled region is the difference between reading it and reading a
marketing column (§8.20), so they are captured here.

`pypdf` answers "is this encrypted?" without a full parse, so the password
ladder can be skipped entirely for the common case.
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO

import pdfplumber
import pypdf

#: A text statement runs to thousands of characters a page. Far below this and
#: the page is an image, so OCR is the only route and parsing would silently
#: return nothing.
SCANNED_CHARS_PER_PAGE = 200

#: Statements run to a few dozen pages. Beyond this something is wrong, and a
#: 2,000-page PDF should not be allowed to exhaust the worker.
MAX_PAGES = 60


class PdfEncryptedError(Exception):
    """The PDF needs a password, or the password given was wrong."""


class PdfUnreadableError(Exception):
    """The bytes are not a PDF we can parse at all."""


@dataclass(frozen=True, slots=True)
class Word:
    text: str
    x0: float
    x1: float
    top: float
    bottom: float
    page: int

    @property
    def centre(self) -> float:
        return (self.x0 + self.x1) / 2


@dataclass(slots=True)
class TextLine:
    """Words sharing a baseline, ordered left to right."""

    page: int
    top: float
    words: list[Word]

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words)

    @property
    def x0(self) -> float:
        return min(w.x0 for w in self.words)

    @property
    def x1(self) -> float:
        return max(w.x1 for w in self.words)


@dataclass(slots=True)
class Page:
    number: int
    width: float
    height: float
    words: list[Word]
    lines: list[TextLine]
    #: Ruling marks: vertical and horizontal strokes, and filled rectangles.
    rulings: list[dict[str, Any]] = field(default_factory=list)

    @property
    def char_count(self) -> int:
        return sum(len(w.text) for w in self.words)


@dataclass(slots=True)
class Document:
    pages: list[Page]
    page_count: int
    #: True when there is far too little text for a statement — an image scan.
    likely_scanned: bool
    truncated: bool = False
    encrypted: bool = False

    @property
    def lines(self) -> list[TextLine]:
        return [line for page in self.pages for line in page.lines]

    @property
    def char_count(self) -> int:
        return sum(page.char_count for page in self.pages)


def _as_stream(source: str | Path | bytes | BinaryIO) -> Any:
    """A seekable handle pdfplumber and pypdf can both read.

    Bytes are wrapped once and rewound between readers; a path is handed over
    as-is so each reader opens its own descriptor.
    """
    if isinstance(source, bytes):
        return io.BytesIO(source)
    return source


def _rewind(handle: Any) -> None:
    if hasattr(handle, "seek"):
        handle.seek(0)


def is_encrypted(source: str | Path | bytes | BinaryIO) -> bool:
    """Cheap encryption check — no full parse, no password needed.

    pypdf answers this reliably. pdfplumber does not: on an encrypted file it
    raises `PdfminerException` with an **empty message**, so classifying by
    exception text silently reports every locked statement as corrupt.
    """
    handle = _as_stream(source)
    try:
        reader = pypdf.PdfReader(handle)
    except pypdf.errors.FileNotDecryptedError:
        return True
    except Exception as exc:
        raise PdfUnreadableError(str(exc) or type(exc).__name__) from exc
    finally:
        _rewind(handle)
    return bool(reader.is_encrypted)


def password_opens(source: str | Path | bytes | BinaryIO, password: str) -> bool:
    """Whether this password decrypts the file. Used by the password ladder.

    Cheaper than a full pdfplumber parse, which matters when up to 25
    candidates are tried.
    """
    handle = _as_stream(source)
    try:
        reader = pypdf.PdfReader(handle)
        if not reader.is_encrypted:
            return True
        return bool(reader.decrypt(password))
    except Exception:
        return False
    finally:
        _rewind(handle)


def group_lines(words: list[Word], tolerance: float = 2.5) -> list[TextLine]:
    """Group words into visual lines by vertical proximity.

    The tolerance absorbs the sub-point baseline jitter that superscripts and
    mixed font sizes introduce within one printed row.
    """
    lines: list[TextLine] = []
    for word in sorted(words, key=lambda w: (w.top, w.x0)):
        if lines and abs(word.top - lines[-1].top) <= tolerance:
            lines[-1].words.append(word)
        else:
            lines.append(TextLine(page=word.page, top=word.top, words=[word]))
    for line in lines:
        line.words.sort(key=lambda w: w.x0)
    return lines


def _rulings(page: Any) -> list[dict[str, Any]]:
    marks: list[dict[str, Any]] = []
    for kind in ("lines", "rects"):
        for mark in getattr(page, kind, []) or []:
            marks.append(
                {
                    "kind": "line" if kind == "lines" else "rect",
                    "x0": mark.get("x0"),
                    "x1": mark.get("x1"),
                    "top": mark.get("top"),
                    "bottom": mark.get("bottom"),
                }
            )
    return marks


def extract_document(
    source: str | Path | bytes | BinaryIO,
    password: str | None = None,
    *,
    max_pages: int = MAX_PAGES,
) -> Document:
    """Open a PDF and return its positioned text.

    Raises `PdfEncryptedError` when a password is needed or wrong, so the
    caller can walk the password ladder, and `PdfUnreadableError` for anything
    that is not a PDF we can read. The distinction comes from pypdf, never from
    pdfplumber's exception text — see `is_encrypted`.
    """
    handle = _as_stream(source)
    encrypted = is_encrypted(handle)

    if encrypted:
        if not password:
            raise PdfEncryptedError("this PDF needs a password")
        if not password_opens(handle, password):
            raise PdfEncryptedError("that password did not open this PDF")

    _rewind(handle)
    try:
        pdf = pdfplumber.open(handle, password=password or "")
    except Exception as exc:
        detail = str(exc) or type(exc).__name__
        if encrypted:
            raise PdfEncryptedError(detail) from exc
        raise PdfUnreadableError(detail) from exc

    pages: list[Page] = []
    with pdf:
        total = len(pdf.pages)
        for index, raw in enumerate(pdf.pages[:max_pages], start=1):
            words = [
                Word(
                    text=w["text"],
                    x0=float(w["x0"]),
                    x1=float(w["x1"]),
                    top=float(w["top"]),
                    bottom=float(w["bottom"]),
                    page=index,
                )
                for w in raw.extract_words(use_text_flow=False, keep_blank_chars=False)
            ]
            pages.append(
                Page(
                    number=index,
                    width=float(raw.width),
                    height=float(raw.height),
                    words=words,
                    lines=group_lines(words),
                    rulings=_rulings(raw),
                )
            )

    chars = sum(p.char_count for p in pages)
    per_page = chars / len(pages) if pages else 0
    return Document(
        pages=pages,
        page_count=total,
        likely_scanned=bool(pages) and per_page < SCANNED_CHARS_PER_PAGE,
        truncated=total > max_pages,
        encrypted=encrypted,
    )
