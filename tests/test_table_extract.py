"""The geometry extractor (§7.2, §10).

Built from synthesised page geometry, not customer PDFs: real statements are
never committed. The layout mirrors what pdfplumber hands back for a typical
Indian savings statement.
"""

from __future__ import annotations

import datetime as dt

from todd.parsers.pdf.extract import Document, Page, TextLine, Word, group_lines
from todd.parsers.table.extract import extract_table, infer_columns

#: Column x-positions: date, narration, amount, balance.
COLUMNS = (40.0, 110.0, 330.0, 430.0)


def _row(top: float, date: str, narration: str, amount: str, balance: str) -> TextLine:
    words: list[Word] = []
    for x, text in zip(COLUMNS, (date, narration, amount, balance), strict=True):
        if not text:
            continue
        words.append(Word(text=text, x0=x, x1=x + 6.0 * len(text), top=top, bottom=top + 9, page=1))
    return TextLine(page=1, top=top, words=words)


def _document(lines: list[TextLine]) -> Document:
    words = [w for line in lines for w in line.words]
    page = Page(number=1, width=595, height=842, words=words, lines=lines)
    return Document(pages=[page], page_count=1, likely_scanned=False)


def _statement() -> Document:
    """Opening 1,00,000.00; two debits and a credit; closing 1,05,000.00."""
    return _document(
        [
            _row(100, "01-04-2026", "UPI/SWIGGY/order", "1,500.00", "98,500.00"),
            _row(120, "03-04-2026", "ATM CASH WITHDRAWAL", "2,000.00", "96,500.00"),
            _row(140, "05-04-2026", "NEFT SALARY CREDIT", "8,500.00", "1,05,000.00"),
        ]
    )


def test_a_statement_is_read_and_the_chain_verifies() -> None:
    result = extract_table(_statement())
    assert result.ok, result.reason
    assert len(result.rows) == 3
    assert result.chain.verdict == "verified"
    assert result.chain.checked == 2


def test_amounts_arrive_as_integer_paise() -> None:
    rows = extract_table(_statement()).rows
    assert [r.amount_paise for r in rows] == [150_000, 200_000, 850_000]
    assert all(isinstance(r.amount_paise, int) for r in rows)


def test_direction_comes_from_the_balance_chain_not_a_label() -> None:
    """No Dr/Cr marker anywhere: the falling balance is what makes these
    debits, and the rising one a credit."""
    rows = extract_table(_statement()).rows
    assert [r.direction for r in rows] == ["debit", "debit", "credit"]


def test_the_opening_balance_is_derived_from_the_printed_chain() -> None:
    result = extract_table(_statement())
    assert result.opening_printed_paise == 10_000_000
    assert result.closing_printed_paise == 10_500_000


def test_an_icici_date_glued_to_its_time_is_still_a_transaction() -> None:
    """§8.19 — `07-04-26` + `1012` arrives as `07-04-261012`. A strict parser
    drops the row and folds the whole transaction into its neighbour."""
    document = _document(
        [
            _row(100, "01-04-2026", "UPI/SWIGGY", "1,500.00", "98,500.00"),
            _row(120, "07-04-261012", "ATM CASH", "2,000.00", "96,500.00"),
            _row(140, "09-04-2026", "NEFT SALARY", "8,500.00", "1,05,000.00"),
        ]
    )
    result = extract_table(document)
    assert len(result.rows) == 3
    assert result.rows[1].txn_date == dt.date(2026, 4, 7)
    assert result.ok


def test_a_non_statement_yields_zero_rows() -> None:
    """§10 — a document that merely contains dates and numbers must produce
    nothing, not a plausible-looking ledger. These rows span ten months, so
    they cannot be one account's statement period."""
    document = _document(
        [
            _row(100, "01-01-2020", "SMA 0 classification", "0.00", ""),
            _row(120, "01-05-2020", "SMA 2 classification", "29.00", ""),
            _row(140, "01-10-2020", "NPA standard", "93.00", ""),
        ]
    )
    result = extract_table(document)
    assert result.rows == []
    assert not result.ok
    assert "statement period" in (result.reason or "")


def test_an_empty_document_reports_why() -> None:
    result = extract_table(_document([]))
    assert result.rows == []
    assert not result.ok
    assert "no line" in (result.reason or "")


def test_columns_are_found_by_gaps_not_by_word_edges() -> None:
    """§8.12 — clustering word edges yields a boundary per word and the
    columns dissolve. Four columns means three corridors."""
    lines = _statement().lines
    boundaries = infer_columns(lines)
    assert len(boundaries) == 3
    assert all(boundaries[i] < boundaries[i + 1] for i in range(len(boundaries) - 1))


def test_wrapped_narration_is_folded_into_its_row() -> None:
    """§8.13 — a continuation line carries neither a standalone amount nor a
    standalone date."""
    continuation = TextLine(
        page=1,
        top=92,
        words=[Word(text="itsnevatha@okaxis", x0=110.0, x1=220.0, top=92, bottom=101, page=1)],
    )
    document = _document(
        [
            continuation,
            _row(100, "01-04-2026", "UPI/NEVATHA/Rent", "1,500.00", "98,500.00"),
            _row(120, "03-04-2026", "ATM CASH", "2,000.00", "96,500.00"),
        ]
    )
    rows = extract_table(document).rows
    assert "itsnevatha@okaxis" in rows[0].description


def test_a_header_line_is_not_folded_into_the_first_transaction() -> None:
    """Without the "no standalone amount or date" rule,
    `Statement From: 01/04/2026 ... Opening Balance 25,000.00` becomes
    narration on row one."""
    # A real header line: its words all sit in the narration column, so it is
    # not a transaction row — but it does carry a date and an amount, which is
    # exactly what must stop it being folded into the row below.
    header = TextLine(
        page=1,
        top=92,
        words=[
            Word(text=t, x0=110.0 + 60 * i, x1=160.0 + 60 * i, top=92, bottom=101, page=1)
            for i, t in enumerate(
                ["Statement", "From:", "01/04/2026", "Opening", "Balance", "25,000.00"]
            )
        ],
    )
    document = _document(
        [
            header,
            _row(100, "01-04-2026", "UPI/SWIGGY", "1,500.00", "98,500.00"),
            _row(120, "03-04-2026", "ATM CASH", "2,000.00", "96,500.00"),
        ]
    )
    rows = extract_table(document).rows
    assert all("Statement From" not in r.description for r in rows)


def test_a_reference_number_is_not_read_as_a_rupee_amount() -> None:
    """§8.20 — a reference read as an amount produced a ₹14 billion row."""
    document = _document(
        [
            _row(100, "01-04-2026", "UPI/SWIGGY", "1,500.00", "98,500.00"),
            _row(120, "03-04-2026", "REF 987654321098765", "2,000.00", "96,500.00"),
        ]
    )
    rows = extract_table(document).rows
    assert all(r.amount_paise < 100_000_000_00 for r in rows)


def test_grouping_words_into_lines_tolerates_baseline_jitter() -> None:
    words = [
        Word(text="A", x0=10, x1=20, top=100.0, bottom=109, page=1),
        Word(text="B", x0=30, x1=40, top=101.5, bottom=110, page=1),
        Word(text="C", x0=10, x1=20, top=140.0, bottom=149, page=1),
    ]
    lines = group_lines(words)
    assert [line.text for line in lines] == ["A B", "C"]
