"""The nightly Veeam backup window, which throttles disk I/O (#390).

A run is flagged, never dropped, when any part of it falls between 00:00 and
11:00 local time. The snapshot has started at 00:10-00:52 and has run past 08:00.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta

WINDOW_END_HOUR = 11


def overlaps_window(start: datetime, end: datetime) -> bool:
    """True if [start, end] touches 00:00-11:00 on any day, in the datetimes' own zone."""
    if start.tzinfo is None or end.tzinfo is None:
        raise ValueError("start and end must be timezone-aware local times")
    day = start.date()
    while day <= end.date():
        window_start = datetime.combine(day, time(0), tzinfo=start.tzinfo)
        window_end = window_start + timedelta(hours=WINDOW_END_HOUR)
        if start < window_end and end >= window_start:
            return True
        day += timedelta(days=1)
    return False
