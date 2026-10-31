"""Indian date formats and IST helpers.

Statements are printed in IST; the database stores UTC. Everything that decides
"which month is this" must do so in IST, or a transaction just before midnight
lands in the wrong month.
"""

from __future__ import annotations

import datetime as dt
import re
from itertools import pairwise
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

MONTH_ABBR = (
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
)  # fmt: skip


def today_ist() -> dt.date:
    return dt.datetime.now(tz=IST).date()


def month_key(value: dt.date) -> str:
    """`YYYY-MM`, the key the report and the web app agree on."""
    return f"{value.year:04d}-{value.month:02d}"


def _shift_month(year: int, month: int, delta: int) -> tuple[int, int]:
    index = year * 12 + (month - 1) + delta
    return index // 12, index % 12 + 1


def last_complete_months(count: int, *, today: dt.date | None = None) -> list[str]:
    """The `count` whole months ending *last* month, oldest first.

    Statements arrive in arrears: on the 22nd, the newest statement a bank has
    emailed covers the month before. A window that includes the current month
    opens on an almost empty report, which reads as "Todd found nothing".
    """
    if count < 1:
        raise ValueError("count must be at least 1")
    anchor = today or today_ist()
    year, month = _shift_month(anchor.year, anchor.month, -1)
    keys: list[str] = []
    for offset in range(count - 1, -1, -1):
        y, m = _shift_month(year, month, -offset)
        keys.append(f"{y:04d}-{m:02d}")
    return keys


MONTH_NAME = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)  # fmt: skip


def month_name(key: str) -> str:
    """ "2026-08" -> "August". Written server-side so two clients cannot differ."""
    return MONTH_NAME[int(key.split("-")[1]) - 1]


def preceding_months(month_keys: list[str]) -> list[str]:
    """The window of the same length immediately before `month_keys`.

    This is what a trend compares against: Apr–Sep is judged against Oct–Mar,
    never against "the same months last year". A window of equal length keeps
    the comparison honest when the user switches from 3 months to 12.
    """
    if not month_keys:
        return []
    year, month = (int(part) for part in month_keys[0].split("-"))
    count = len(month_keys)
    keys: list[str] = []
    for offset in range(count, 0, -1):
        y, m = _shift_month(year, month, -offset)
        keys.append(f"{y:04d}-{m:02d}")
    return keys


def window_label(month_keys: list[str]) -> str:
    """ "Apr – Sep", or "Apr 2025 – Mar 2026" when the window spans a year end.

    The server writes this string so two clients cannot phrase it differently.
    """
    if not month_keys:
        return ""
    first_year, first_month = (int(p) for p in month_keys[0].split("-"))
    last_year, last_month = (int(p) for p in month_keys[-1].split("-"))
    first = MONTH_ABBR[first_month - 1]
    last = MONTH_ABBR[last_month - 1]
    if len(month_keys) == 1:
        return f"{first} {first_year}"
    if first_year == last_year:
        return f"{first} – {last}"
    return f"{first} {first_year} – {last} {last_year}"


def gap_label(month_keys: list[str]) -> str:
    """The missing months, as Profile says them: "Jul", "Jul – Sep", "4 months".

    A run of consecutive months is a range because that is how someone thinks
    of it. Scattered months are counted instead: "Missing Apr, Jun, Sep and
    Nov" is a sentence nobody finishes reading, and the action is the same.
    """
    if not month_keys:
        return ""
    ordered = sorted(month_keys)
    names = [MONTH_ABBR[int(key.split("-")[1]) - 1] for key in ordered]
    if len(ordered) == 1:
        return names[0]

    indexes = [int(k[:4]) * 12 + int(k[5:7]) for k in ordered]
    consecutive = all(b - a == 1 for a, b in pairwise(indexes))
    if consecutive:
        return f"{names[0]} – {names[-1]}"
    return f"{len(ordered)} months"


def month_bounds(key: str) -> tuple[dt.date, dt.date]:
    """First and last day of a `YYYY-MM` key, inclusive."""
    year, month = (int(p) for p in key.split("-"))
    start = dt.date(year, month, 1)
    next_year, next_month = _shift_month(year, month, 1)
    end = dt.date(next_year, next_month, 1) - dt.timedelta(days=1)
    return start, end


# --------------------------------------------------------------- statement dates

#: Day-first, always: every Indian bank statement prints DD before MM.
#:
#: The year is matched at an exact width, longest first, rather than with
#: `\d{2,4}`. A greedy year group reads ICICI's `07-04-261012` as the year
#: 2610 and rejects the row (§8.19); trying 4 digits and then 2 reads it as
#: 2026 with `1012` left over as the trailing time.
_NUMERIC_DATES = [
    re.compile(r"^(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})"),
    re.compile(r"^(\d{1,2})[-/.](\d{1,2})[-/.](\d{2})"),
]
_NAMED_DATES = [
    re.compile(r"^(\d{1,2})[-/. ]([A-Za-z]{3,9})[-/. ](\d{4})"),
    re.compile(r"^(\d{1,2})[-/. ]([A-Za-z]{3,9})[-/. ](\d{2})"),
]

_MONTHS: dict[str, int] = {m.lower(): i + 1 for i, m in enumerate(MONTH_ABBR)}
_MONTHS.update(
    {
        "january": 1, "february": 2, "march": 3, "april": 4, "june": 6, "july": 7,
        "august": 8, "september": 9, "sept": 9, "october": 10, "november": 11, "december": 12,
    }
)  # fmt: skip


def parse_statement_date(text: str | None, *, century: int = 2000) -> dt.date | None:
    """A date at the *start* of the text, or None.

    §8.19 — ICICI prints an ATM transaction's date glued to its time:
    `07-04-26` + `1012` arrives as `07-04-261012`. A strict full-string match
    rejects it, the row then fails the "begins with a date" test, and the whole
    transaction is absorbed into its neighbour as narration. Two of six ICICI
    statements failed the balance chain for this reason alone.

    So this matches a *prefix*. The cost is accepting a little trailing junk;
    the benefit is not silently losing transactions.
    """
    if not text:
        return None
    token = text.strip().strip(",")

    for pattern in (*_NUMERIC_DATES, *_NAMED_DATES):
        match = pattern.match(token)
        if not match:
            continue
        day_raw, month_raw, year_raw = match.groups()

        month = _MONTHS.get(month_raw.lower()) if month_raw[0].isalpha() else int(month_raw)
        if month is None or not 1 <= month <= 12:
            continue

        day = int(day_raw)
        if not 1 <= day <= 31:
            continue

        year = int(year_raw)
        if year < 100:
            year += century
        # A statement from 1970 or 2300 is a misread, not a date.
        if not 1990 <= year <= 2100:
            continue

        try:
            return dt.date(year, month, day)
        except ValueError:
            continue  # 31 February and friends
    return None


def looks_like_statement_date(text: str | None) -> bool:
    return parse_statement_date(text) is not None
