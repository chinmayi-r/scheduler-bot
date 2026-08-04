from __future__ import annotations
from datetime import datetime, date, time
import pytz


def now_in_tz(tz_name: str) -> datetime:
    tz = pytz.timezone(tz_name)
    return datetime.now(tz)


def today_in_tz(tz_name: str) -> date:
    return now_in_tz(tz_name).date()


def parse_hhmm(hhmm: str) -> time:
    """'08:30' -> time(8, 30). Raises ValueError on bad input."""
    hh, mm = hhmm.strip().split(":")
    h, m = int(hh), int(mm)
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError(f"Invalid time: {hhmm}")
    return time(h, m)


def valid_timezone(tz_name: str) -> bool:
    try:
        pytz.timezone(tz_name)
        return True
    except Exception:
        return False
