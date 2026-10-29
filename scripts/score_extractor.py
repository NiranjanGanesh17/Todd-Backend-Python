"""Score the geometry extractor against real statements.

§13 step 4: "run it against real statements and score with the balance chain."
The chain is the scoring function — it is a per-row arithmetic proof, so a
statement that verifies needs no model and costs nothing.

Prints aggregates only. Never narration, never a counterparty: these are real
statements and the terminal is not a safe place for them.

    uv run python scripts/score_extractor.py <dir-or-pdf> [...]
"""

from __future__ import annotations

import pathlib
import sys
from collections import Counter

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from todd.parsers.money import format_paise
from todd.parsers.pdf.extract import (
    PdfEncryptedError,
    PdfUnreadableError,
    extract_document,
)
from todd.parsers.table.extract import extract_table


def collect(targets: list[str]) -> list[pathlib.Path]:
    found: list[pathlib.Path] = []
    for target in targets:
        path = pathlib.Path(target)
        if path.is_dir():
            found.extend(sorted(path.rglob("*.pdf")))
        elif path.suffix.lower() == ".pdf":
            found.append(path)
    return found


def group_of(name: str) -> str:
    """A coarse label so results can be read per bank without naming accounts."""
    lowered = name.lower()
    if lowered.startswith("statement_") and "mth" in lowered:
        return "Bank of Baroda"
    if "retail_platinum" in lowered or lowered.startswith("4035"):
        return "ICICI credit card"
    if lowered.startswith("5874"):
        return "ICICI savings"
    if "hdfc" in lowered:
        return "HDFC"
    return "other / not a statement"


def main() -> int:
    targets = sys.argv[1:]
    if not targets:
        print(__doc__)
        return 2

    files = collect(targets)
    if not files:
        print("no PDFs found")
        return 2

    verdicts: Counter[str] = Counter()
    per_group: dict[str, list[str]] = {}

    print(f"{'file':34} {'group':20} {'pg':>3} {'rows':>5} {'chain':>12}  totals")
    print("-" * 104)

    for path in files:
        label = path.name[:32]
        group = group_of(path.name)
        try:
            document = extract_document(path)
        except PdfEncryptedError:
            verdicts["locked"] += 1
            per_group.setdefault(group, []).append("locked")
            print(f"{label:34} {group:20} {'—':>3} {'—':>5} {'LOCKED':>12}  needs a password")
            continue
        except PdfUnreadableError as exc:
            verdicts["unreadable"] += 1
            per_group.setdefault(group, []).append("unreadable")
            print(f"{label:34} {group:20} {'—':>3} {'—':>5} {'unreadable':>12}  {exc}")
            continue

        result = extract_table(document)
        chain = result.chain
        if not result.rows:
            verdict = "no rows"
        elif chain.passed:
            verdict = "VERIFIED"
        elif chain.checked:
            verdict = f"broken({len(chain.breaks)})"
        else:
            verdict = "no balance col"

        verdicts[verdict.split("(")[0]] += 1
        per_group.setdefault(group, []).append(verdict)

        totals = ""
        if result.rows:
            totals = (
                f"dr {format_paise(result.debits_paise, symbol=False):>14}  "
                f"cr {format_paise(result.credits_paise, symbol=False):>14}"
            )
        print(
            f"{label:34} {group:20} {result.page_count:>3} {len(result.rows):>5} "
            f"{verdict:>12}  {totals}"
        )

    print("-" * 104)
    print("\nby bank:")
    for group, results in sorted(per_group.items()):
        verified = sum(1 for r in results if r == "VERIFIED")
        print(f"  {group:22} {verified}/{len(results)} verified by the balance chain")

    print("\noverall:", dict(verdicts))
    total = sum(verdicts.values())
    free = verdicts.get("VERIFIED", 0)
    if total:
        print(f"needs no model: {free}/{total} = {free / total:.0%}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
