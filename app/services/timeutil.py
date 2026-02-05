from __future__ import annotations
from datetime import datetime, date, time
import pytz

def now_in_tz(tz_name: str) -> datetime:
    tz = pytz.timezone(tz_name)
    return datetime.now(tz)

def today_in_tz(tz_name: str) -> date:
    return now_in_tz(tz_name).date()

def is_after_local_hour(tz_name: str, hour_24: int) -> bool:
    return now_in_tz(tz_name).hour >= hour_24
