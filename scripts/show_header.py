"""What the parser actually sees on page one of a statement.

A PDF has no lines. It has words at positions, and `group_lines` turns those
back into lines by vertical proximity — so a sentence printed in one piece can
arrive wrapped, or split across columns, and the shape differs between two
statements of the same account. When Profile shows an account number that is
not on the statement, this is how to see why.

Prints the header the way `detect_account` receives it, then what each pass of
the reader makes of it. Long runs of digits are masked to their last four
before printing: that is all the application keeps (§12), and it is the part
this is diagnosing, so the output is safe to paste.

    uv run python scripts/show_header.py ~/Downloads/Statement_JUL2026.pdf

The password is asked for, never echoed, and never stored. Nothing is written
and the ledger is not touched.
"""

from __future__ import annotations

import getpass
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from todd.parsers.institution import (
    detect_account_type,
    detect_institution,
    detect_masked_number,
    header_of,
)
from todd.parsers.pdf.extract import PdfEncryptedError, extract_document, is_encrypted

#: Everything but the last four of any long number. The four are what the
#: account is recognised by; the rest is not ours and not needed here.
_LONG_NUMBER = re.compile(r"\d{5,}")


def _mask(line: str) -> str:
    return _LONG_NUMBER.sub(lambda m: "·" * (len(m.group()) - 4) + m.group()[-4:], line)


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit("usage: uv run python scripts/show_header.py <statement.pdf>")
    path = pathlib.Path(sys.argv[1]).expanduser()
    data = path.read_bytes()

    password = None
    if is_encrypted(data):
        password = getpass.getpass(f"password for {path.name}: ")
    try:
        document = extract_document(data, password)
    except PdfEncryptedError as exc:
        sys.exit(f"{exc}")

    if not document.pages:
        sys.exit("no pages")

    lines = [line.text for line in document.pages[0].lines]
    header = header_of(lines)
    print(f"\n{path.name}: {document.page_count} page(s), {len(lines)} line(s) on page one")
    print(f"the reader stops at the table, {len(header)} line(s) in:\n")
    for i, line in enumerate(header):
        print(f"{i:3d} | {_mask(line)}")
    rest = len(lines) - len(header)
    if rest > 0:
        print(f"    --- the table, and {rest - 1} more line(s) below it ---")
        print(f"{len(header):3d} | {_mask(lines[len(header)])}")
    text = "\n".join(header).lower()
    print("\nread as:")
    print(f"  institution   {detect_institution(text).value}")
    print(f"  account type  {(detect_account_type(text) or 'not stated (savings)')}")
    print(f"  last four     {detect_masked_number(header) or 'none found'}")


if __name__ == "__main__":
    main()
