"""Indian date formats and IST helpers.

Statements are printed in IST; the database stores UTC. Everything that decides
"which month is this" must do so in IST, or a transaction just before midnight
lands in the wrong month.
"""

from __future__ import annotations

import datetime as dt
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


def month_bounds(key: str) -> tuple[dt.date, dt.date]:
    """First and last day of a `YYYY-MM` key, inclusive."""
    year, month = (int(p) for p in key.split("-"))
    start = dt.date(year, month, 1)
    next_year, next_month = _shift_month(year, month, 1)
    end = dt.date(next_year, next_month, 1) - dt.timedelta(days=1)
    return start, end
