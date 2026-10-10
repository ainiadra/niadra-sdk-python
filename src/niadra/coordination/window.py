"""A contact window (`spec/suppression-list.md`, 6.4): the local hours a suppression holds in, as when a
customer says "don't call before 9 am" (from 00:00 to 09:00 on the voice channel). Outside them the contact
may go.

- `from` is inclusive and `to` exclusive: at exactly 09:00 the window from 00:00 to 09:00 is over;
- a window whose `to` is not after its `from` crosses midnight and belongs to the day it starts on: a Friday
  window from 22:00 to 06:00 still holds at 01:00 on Saturday;
- `days` are ISO weekdays (1 is Monday) the window starts on, or None for every day;
- local time follows the zone's rules (the IANA time zone database, daylight saving included).

The spec's vectors (`spec/vectors/contact-window.v0.json`) check this module.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_CLOCK = re.compile(r"^([01][0-9]|2[0-3]):([0-5][0-9])$")
_ZONE = re.compile(r"^[A-Za-z_]+(/[A-Za-z0-9_+-]+)*$")


class InvalidWindowError(ValueError):
    """Not a window: a bad clock time, a start equal to its end, a zone that is not an IANA name (or that this
    machine's time zone database lacks), or a day that is not an ISO weekday."""

    code = "invalid_window"


def window_until(window: Any, at: datetime) -> datetime | None:
    """When the window `at` falls in ends, in UTC, or None when `at` is outside it. `window` is a
    `ContactWindow` or its JSON (`from`, `to`, `tz`, `days`)."""
    start, end, tz, days = _parts(window)
    zone = _zone(tz)
    local = at.astimezone(zone)
    now = local.time().replace(tzinfo=None)
    crosses = end <= start
    if not crosses:
        inside, first = start <= now < end, local.date()
    elif now >= start:
        inside, first = True, local.date()
    elif now < end:
        inside, first = True, local.date() - timedelta(days=1)
    else:
        inside, first = False, local.date()
    if not inside or (days and first.isoweekday() not in days):
        return None
    last = first + timedelta(days=1) if crosses else first
    return datetime.combine(last, end, tzinfo=zone).astimezone(timezone.utc)


def _parts(window: Any) -> tuple[time, time, str, tuple[int, ...]]:
    if isinstance(window, Mapping):
        start, end, tz, days = window.get("from"), window.get("to"), window.get("tz"), window.get("days")
    else:
        start, end, tz, days = window.from_, window.to, window.tz, window.days
    found: Sequence[Any] = days or ()
    if any(not isinstance(d, int) or isinstance(d, bool) or not 1 <= d <= 7 for d in found):
        raise InvalidWindowError("days are ISO weekdays, 1 (Monday) to 7 (Sunday)")
    begins, ends = _clock(start), _clock(end)
    if begins == ends:
        raise InvalidWindowError("a window starts before it ends")
    if not isinstance(tz, str) or not _ZONE.match(tz):
        raise InvalidWindowError(f"{tz} is not an IANA time zone")
    return begins, ends, tz, tuple(found)


def _zone(tz: str) -> ZoneInfo:
    try:
        return ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError):
        raise InvalidWindowError(f"{tz} is not a time zone this machine knows") from None


def _clock(text: Any) -> time:
    found = _CLOCK.match(str(text))
    if found is None:
        raise InvalidWindowError(f"{text} is not a time as HH:MM")
    return time(int(found.group(1)), int(found.group(2)))
