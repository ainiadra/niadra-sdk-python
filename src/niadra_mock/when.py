"""The emulator's reading of `filters.when`: common time phrases in English, Portuguese and Spanish.

A small subset of what the API reads, enough to write tests against: today, yesterday, this or last
week, this or last month, the last N days, and a month by name. Weeks start on Monday and every
period ends where the next one starts. A phrase outside the subset is not read, and the search
lists `when` in `ignored`, as the API does.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timedelta

Window = tuple[datetime, datetime]

_TODAY = {"today", "hoje", "hoy"}
_YESTERDAY = {"yesterday", "ontem", "ayer"}
_THIS_WEEK = {"this week", "esta semana"}
_LAST_WEEK = {"last week", "semana passada", "a semana passada", "semana pasada", "la semana pasada"}
_THIS_MONTH = {"this month", "este mes", "este mês"}
_LAST_MONTH = {"last month", "mes passado", "o mes passado", "mes pasado", "el mes pasado"}
_LAST_DAYS = re.compile(r"^(?:(?:the )?last|(?:os )?ultimos|(?:los )?ultimos) (\d{1,3}) (?:days|dias)$")
_MONTHS = {
    **dict.fromkeys(("january", "janeiro", "enero"), 1),
    **dict.fromkeys(("february", "fevereiro", "febrero"), 2),
    **dict.fromkeys(("march", "marco", "marzo"), 3),
    **dict.fromkeys(("april", "abril"), 4),
    **dict.fromkeys(("may", "maio", "mayo"), 5),
    **dict.fromkeys(("june", "junho", "junio"), 6),
    **dict.fromkeys(("july", "julho", "julio"), 7),
    **dict.fromkeys(("august", "agosto"), 8),
    **dict.fromkeys(("september", "setembro", "septiembre"), 9),
    **dict.fromkeys(("october", "outubro", "octubre"), 10),
    **dict.fromkeys(("november", "novembro", "noviembre"), 11),
    **dict.fromkeys(("december", "dezembro", "diciembre"), 12),
}
_MONTH_PREFIX = re.compile(r"^(?:in|em|en|no|na|de)\s+")


def _plain(phrase: str) -> str:
    folded = unicodedata.normalize("NFKD", phrase.strip().lower())
    return " ".join("".join(c for c in folded if not unicodedata.combining(c)).split())


def _month_start(year: int, month: int, tz: datetime) -> datetime:
    return tz.replace(year=year, month=month, day=1, hour=0, minute=0, second=0, microsecond=0)


def _next_month(start: datetime) -> datetime:
    return (
        start.replace(year=start.year + 1, month=1)
        if start.month == 12
        else start.replace(month=start.month + 1)
    )


def read_when(phrase: str, now: datetime) -> Window | None:
    """The `[since, until)` a phrase means, read from `now`; None when the emulator cannot read it."""
    text = _plain(phrase)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if text in _TODAY:
        return midnight, midnight + timedelta(days=1)
    if text in _YESTERDAY:
        return midnight - timedelta(days=1), midnight
    monday = midnight - timedelta(days=midnight.weekday())
    if text in _THIS_WEEK:
        return monday, monday + timedelta(days=7)
    if text in _LAST_WEEK:
        return monday - timedelta(days=7), monday
    first = midnight.replace(day=1)
    if text in {_plain(p) for p in _THIS_MONTH}:
        return first, _next_month(first)
    if text in _LAST_MONTH:
        previous = (first - timedelta(days=1)).replace(day=1)
        return previous, first
    if (match := _LAST_DAYS.match(text)) is not None:
        return midnight - timedelta(days=int(match.group(1))), midnight + timedelta(days=1)
    month = _MONTHS.get(_MONTH_PREFIX.sub("", text))
    if month is not None:
        # The latest such month that has started: "in March" in September is this year's March.
        year = now.year if month <= now.month else now.year - 1
        start = _month_start(year, month, now)
        return start, _next_month(start)
    return None
