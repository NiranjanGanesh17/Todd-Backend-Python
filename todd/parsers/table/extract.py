"""Geometry table extractor — the free engine, tried before the model.

Ported from `tools/table-extract/extract.py` in the TypeScript repo, where it
was measured against real statements: Bank of Baroda 6/6 verified by the
balance chain, ICICI savings 4/6, ICICI credit card 0/6. Where it verified, its
totals matched the LLM to the paise across 200 rows.

Nothing here knows the name of a bank, a column caption, or a date format
peculiar to one institution. The only assumptions hold of every statement:

  * a transaction row begins with a date
  * amounts look like amounts
  * a column is a vertical corridor of page that no row writes into
  * where a running balance is printed, consecutive balances differ by exactly
    the row's amount

That last one is the important one: a per-row arithmetic proof that, unlike an
aggregate total, cannot be satisfied by accident. The check itself lives in
`parsers.reconcile` so the extractor and the gate cannot drift apart.

Three changes from the ported original:
  * dates go through `parse_statement_date`, which matches a prefix, so
    ICICI's `07-04-261012` is read rather than dropped (§8.19)
  * amounts go through the shared `parse_indian_amount`
  * an implausibly large amount is rejected, because a reference number read
    as an amount produced a ₹14 billion row (§8.20)
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass, field

from todd.parsers.dates import parse_statement_date
from todd.parsers.money import parse_indian_amount
from todd.parsers.pdf.extract import Document, TextLine
from todd.parsers.reconcile import ChainCheck, check_running_balance

#: A rupee, in paise. The chain tolerance: statements round, we do not chase it.
RUPEE = 100

#: No retail statement line is ₹10 crore. Above this the "amount" is a
#: reference number that landed in an amount column (§8.20).
MAX_PLAUSIBLE_PAISE = 100_000_000_00

#: The narrowest vertical corridor that still separates two columns.
MIN_GUTTER = 4.0

#: One statement covers one period — a month, or a quarter at worst. Rows
#: spanning far longer are not one account's chronology: they are a table that
#: merely looks like one. ICICI's "Most Important Terms and Conditions" carries
#: an illustrative SMA/NPA table whose rows read as ten monthly transactions of
#: ₹0 to ₹93, and §10 requires a non-statement to yield zero rows.
MAX_PERIOD_DAYS = 100

#: Below this a "table" is a coincidence, not a transaction list.
MIN_ROWS = 2


@dataclass(slots=True)
class ExtractedRow:
    txn_date: dt.date
    description: str
    amount_paise: int
    direction: str
    balance_paise: int | None
    page: int
    line_index: int
    source_lines: list[str] = field(default_factory=list, repr=False)


@dataclass(slots=True)
class ExtractionResult:
    ok: bool
    rows: list[ExtractedRow]
    page_count: int
    chain: ChainCheck
    columns: list[str]
    debits_paise: int
    credits_paise: int
    opening_printed_paise: int | None
    closing_printed_paise: int | None
    reason: str | None = None
    diagnostics: list[str] = field(default_factory=list)


# ----------------------------------------------------------------- row finding


def _amount_of(text: str) -> int | None:
    """Paise, or None when the text is not a plausible statement amount."""
    parsed = parse_indian_amount(text)
    if parsed is None:
        return None
    if abs(parsed.paise) > MAX_PLAUSIBLE_PAISE:
        return None
    return parsed.paise


def looks_like_data_row(line: TextLine) -> bool:
    """A transaction row, identified by content rather than by any header.

    It begins with a date and carries at least one amount. Requiring the date
    to be the *first* token is what separates a transaction from a summary line
    that merely mentions a date in prose.
    """
    if not line.words:
        return False
    if parse_statement_date(line.words[0].text) is None:
        return False
    return any(_amount_of(w.text) is not None for w in line.words[1:])


# ------------------------------------------------------------ column inference


def infer_columns(rows: Sequence[TextLine], min_gutter: float = MIN_GUTTER) -> list[float]:
    """Column boundaries, from the whitespace between the columns.

    §8.12 — clustering word *edges* does not work: every word has two of them,
    so a four-row table yields a boundary per word and the columns dissolve.
    What separates columns is a vertical corridor of page that no row writes
    into. Projecting every word's x-range onto one axis and finding the gaps
    locates those corridors, and the column count falls out of the document
    instead of being assumed.
    """
    if not rows:
        return []

    left = min(w.x0 for r in rows for w in r.words)
    right = max(w.x1 for r in rows for w in r.words)
    if right <= left:
        return []

    span = int(right - left) + 2  # 1pt resolution is finer than any real gutter
    occupied = bytearray(span)
    for row in rows:
        for word in row.words:
            start = max(0, int(word.x0 - left))
            end = min(span, int(word.x1 - left) + 1)
            for i in range(start, end):
                occupied[i] = 1

    boundaries: list[float] = []
    run_start: int | None = None
    for i in range(span):
        if not occupied[i]:
            if run_start is None:
                run_start = i
        else:
            if run_start is not None and i - run_start >= min_gutter:
                boundaries.append(left + (run_start + i) / 2)
            run_start = None
    return boundaries


def cells_for(line: TextLine, boundaries: Sequence[float]) -> list[str]:
    """Slice a line's words into the inferred columns."""
    cells = [""] * (len(boundaries) + 1)
    for word in line.words:
        index = 0
        while index < len(boundaries) and word.centre >= boundaries[index]:
            index += 1
        cells[index] = f"{cells[index]} {word.text}".strip()
    return cells


def classify_columns(rows: Sequence[list[str]]) -> list[str]:
    """Label each column `date`, `amount`, `text` or `empty` by its content.

    Content, not captions: a column whose cells parse as dates is the date
    column whether the bank titled it "Date", "Txn Date" or nothing at all.
    """
    if not rows:
        return []
    width = max(len(r) for r in rows)
    kinds: list[str] = []
    for i in range(width):
        values = [r[i] for r in rows if i < len(r) and r[i].strip()]
        if not values:
            kinds.append("empty")
            continue
        dates = sum(1 for v in values if parse_statement_date(v) is not None)
        amounts = sum(1 for v in values if _amount_of(v) is not None)
        if dates / len(values) > 0.8:
            kinds.append("date")
        elif amounts / len(values) > 0.8:
            kinds.append("amount")
        else:
            kinds.append("text")
    return kinds


# ------------------------------------------------------------------- direction


def direction_from_chain(
    amount_paise: int, balance: int | None, previous_balance: int | None
) -> str | None:
    """Which way the money went, decided by arithmetic rather than a label.

    If the printed balance fell by the amount it was a debit; if it rose, a
    credit. Immune to ICICI printing deposits before withdrawals, to Dr/Cr
    markers being absent, and to our own guess about column order.
    """
    if balance is None or previous_balance is None:
        return None
    delta = balance - previous_balance
    if abs(abs(delta) - amount_paise) > RUPEE:
        return None  # more than a rupee out: the chain is broken here
    return "debit" if delta < 0 else "credit"


def resolve_column_directions(rows: list[ExtractedRow]) -> None:
    """Settle `col3`-style placeholders left by two-value-column statements.

    Where the balance chain decided some rows, the value column that coincides
    with falling balances is the debit column — inferred, because statements
    disagree about which column comes first. Only when the chain decided
    nothing does this fall back on position.
    """
    pending = {r.direction for r in rows if r.direction.startswith("col")}
    if not pending:
        return

    mapping: dict[str, str] = {}

    # Count, per placeholder column, how the chain-decided rows around it went.
    decided = [r for r in rows if r.direction in ("debit", "credit")]
    if decided:
        debit_cols: dict[str, int] = dict.fromkeys(pending, 0)
        credit_cols: dict[str, int] = dict.fromkeys(pending, 0)
        for row in rows:
            if not row.direction.startswith("col"):
                continue
            # The nearest chain-decided neighbour is the best evidence available
            # for what this column means.
            nearest = min(decided, key=lambda d: abs(d.line_index - row.line_index), default=None)
            if nearest is None:
                continue
            if nearest.direction == "debit":
                debit_cols[row.direction] += 1
            else:
                credit_cols[row.direction] += 1
        for token in pending:
            if debit_cols[token] or credit_cols[token]:
                mapping[token] = "debit" if debit_cols[token] >= credit_cols[token] else "credit"

    # Convention for anything still undecided: the rightmost value column is
    # the debit column.
    rightmost = max(int(t[3:]) for t in pending)
    for token in pending:
        mapping.setdefault(token, "debit" if int(token[3:]) == rightmost else "credit")

    for row in rows:
        if row.direction in mapping:
            row.direction = mapping[row.direction]


# ---------------------------------------------------------------- continuations


def is_continuation(line: TextLine, text_span: tuple[float, float]) -> bool:
    """Whether a line is the rest of the narration above it.

    §8.13 — two content tests, no captions. Real narration carries neither a
    standalone amount nor a standalone date; a line that does is a header or a
    summary, which is how `Statement From: 01/04/2026 … Opening Balance
    25,000.00` stops being folded into the first transaction. It must also sit
    inside the description column's horizontal span, so a marketing column
    printed beside the table is never mistaken for narration.
    """
    for word in line.words:
        if _amount_of(word.text) is not None or parse_statement_date(word.text) is not None:
            return False
    left, right = text_span
    inside = [w for w in line.words if w.x1 >= left and w.x0 <= right]
    return bool(inside) and len(inside) >= len(line.words) / 2


def attach_continuations(
    rows: list[ExtractedRow],
    all_lines: Sequence[TextLine],
    text_span: tuple[float, float],
) -> None:
    """Fold wrapped narration into the row below it.

    A statement line with no date is not a transaction; it is the rest of a
    neighbouring description. This is what turns

        NEVATHA
        UPI/NEVATHA/itsnevatha@oka/Rent/ICICI
        01-09-2026 10,000.00 73,449.48

    into one row carrying its full narration.
    """
    if not rows:
        return
    by_page: dict[int, list[TextLine]] = {}
    for line in all_lines:
        by_page.setdefault(line.page, []).append(line)

    for row in rows:
        page_lines = by_page.get(row.page, [])
        anchor = next(
            (i for i, line in enumerate(page_lines) if line.text in row.source_lines), None
        )
        if anchor is None:
            continue
        extra: list[str] = []
        for line in reversed(page_lines[max(0, anchor - 3) : anchor]):
            if looks_like_data_row(line) or not is_continuation(line, text_span):
                break
            extra.insert(0, line.text)
        if extra:
            row.description = " ".join([*extra, row.description]).strip()


def _implausible(rows: Sequence[ExtractedRow]) -> str | None:
    """Why these rows cannot be one statement's transaction table, or None.

    Both tests are bank-agnostic. Neither looks at a caption, a bank name or a
    document title — only at whether the rows could belong to one account over
    one period.
    """
    if len(rows) < MIN_ROWS:
        return f"only {len(rows)} plausible transaction rows: not a statement table"

    span = (max(r.txn_date for r in rows) - min(r.txn_date for r in rows)).days
    if span > MAX_PERIOD_DAYS:
        return (
            f"rows span {span} days, more than one statement period: "
            "this table is not one account's chronology"
        )
    return None


# -------------------------------------------------------------------- assembly


def extract_table(document: Document) -> ExtractionResult:
    """Read a statement's transaction table out of its positioned text."""
    diagnostics: list[str] = []
    all_lines = document.lines
    data_lines = [line for line in all_lines if looks_like_data_row(line)]

    diagnostics.append(
        f"{document.page_count} pages, {len(all_lines)} lines, "
        f"{len(data_lines)} look like transactions"
    )
    if document.likely_scanned:
        diagnostics.append("likely scanned: too little text for a statement")

    empty = ChainCheck(checked=0, breaks=[])
    if not data_lines:
        return ExtractionResult(
            ok=False,
            rows=[],
            page_count=document.page_count,
            chain=empty,
            columns=[],
            debits_paise=0,
            credits_paise=0,
            opening_printed_paise=None,
            closing_printed_paise=None,
            reason="no line in this PDF begins with a date and carries an amount",
            diagnostics=diagnostics,
        )

    boundaries = infer_columns(data_lines)
    celled = [cells_for(line, boundaries) for line in data_lines]
    kinds = classify_columns(celled)
    diagnostics.append(f"columns: {' | '.join(kinds)}")

    amount_cols = [i for i, k in enumerate(kinds) if k == "amount"]
    text_cols = [i for i, k in enumerate(kinds) if k == "text"]
    date_cols = [i for i, k in enumerate(kinds) if k == "date"]

    if not amount_cols or not date_cols:
        return ExtractionResult(
            ok=False,
            rows=[],
            page_count=document.page_count,
            chain=empty,
            columns=kinds,
            debits_paise=0,
            credits_paise=0,
            opening_printed_paise=None,
            closing_printed_paise=None,
            reason=f"could not find both a date and an amount column (got {kinds})",
            diagnostics=diagnostics,
        )

    # The rightmost amount column is the running balance *if* its values move by
    # the other columns' amounts. That is checked by the chain, never assumed.
    balance_col = amount_cols[-1] if len(amount_cols) > 1 else None
    value_cols = [c for c in amount_cols if c != balance_col]

    rows: list[ExtractedRow] = []
    previous_balance: int | None = None

    for position, (line, cells) in enumerate(zip(data_lines, celled, strict=True)):
        txn_date = parse_statement_date(cells[date_cols[0]])
        if txn_date is None:
            continue

        values = [
            (col, _amount_of(cells[col]))
            for col in value_cols
            if col < len(cells) and _amount_of(cells[col]) is not None
        ]
        if not values:
            continue
        col, amount = values[0]
        assert amount is not None

        balance = (
            _amount_of(cells[balance_col])
            if balance_col is not None and balance_col < len(cells)
            else None
        )

        direction = direction_from_chain(abs(amount), balance, previous_balance)
        if direction is None:
            marker = parse_indian_amount(cells[col])
            if marker is not None and marker.marker:
                direction = "debit" if marker.is_debit else "credit"
            elif len(value_cols) >= 2:
                # Two columns and no chain yet: defer, and decide once for the
                # whole statement after the loop.
                direction = f"col{col}"
            else:
                direction = "debit" if amount >= 0 else "credit"

        rows.append(
            ExtractedRow(
                txn_date=txn_date,
                description=" ".join(cells[c] for c in text_cols if c < len(cells)).strip(),
                amount_paise=abs(amount),
                direction=direction,
                balance_paise=balance,
                page=line.page,
                line_index=position,
                source_lines=[line.text],
            )
        )
        if balance is not None:
            previous_balance = balance

    resolve_column_directions(rows)

    edges = [0.0, *boundaries, 10_000.0]
    text_span = (edges[min(text_cols)], edges[max(text_cols) + 1]) if text_cols else (0.0, 10_000.0)
    attach_continuations(rows, all_lines, text_span)

    # A zero-rupee transaction is not a transaction; it is a misread cell.
    rows = [r for r in rows if r.amount_paise > 0]

    reason = _implausible(rows)
    if reason is not None:
        diagnostics.append(reason)
        return ExtractionResult(
            ok=False,
            rows=[],
            page_count=document.page_count,
            chain=empty,
            columns=kinds,
            debits_paise=0,
            credits_paise=0,
            opening_printed_paise=None,
            closing_printed_paise=None,
            reason=reason,
            diagnostics=diagnostics,
        )

    chain = check_running_balance(rows)
    opening = None
    if rows and rows[0].balance_paise is not None:
        signed = -rows[0].amount_paise if rows[0].direction == "debit" else rows[0].amount_paise
        opening = rows[0].balance_paise - signed
    closing = next((r.balance_paise for r in reversed(rows) if r.balance_paise is not None), None)

    return ExtractionResult(
        ok=chain.passed,
        rows=rows,
        page_count=document.page_count,
        chain=chain,
        columns=kinds,
        debits_paise=sum(r.amount_paise for r in rows if r.direction == "debit"),
        credits_paise=sum(r.amount_paise for r in rows if r.direction == "credit"),
        opening_printed_paise=opening,
        closing_printed_paise=closing,
        diagnostics=diagnostics,
    )
