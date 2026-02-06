from __future__ import annotations

from datetime import datetime, timedelta, date
import pytz

from ..db import Checkin, DailyEventIndex, User

def _required_daily_count() -> int:
    return 3  # morning, run, winddown

def _count_required_events(db, user: User, day: date) -> int:
    return (
        db.query(DailyEventIndex)
        .filter(DailyEventIndex.user_id == user.id, DailyEventIndex.day == day)
        .count()
    )

def _count_completed_daily(db, user: User, day: date) -> int:
    return (
        db.query(Checkin)
        .filter(
            Checkin.user_id == user.id,
            Checkin.day == day,
            Checkin.kind == "daily",
            Checkin.responded_at.is_not(None),
        )
        .count()
    )

def _count_completed_event_photos(db, user: User, day: date) -> int:
    q = (
        db.query(Checkin)
        .filter(
            Checkin.user_id == user.id,
            Checkin.day == day,
            Checkin.kind == "event",
            Checkin.responded_at.is_not(None),
        )
        .filter((Checkin.photo_file_id.is_not(None)) | (Checkin.response_text.is_not(None)))
    )
    return q.count()

def compute_day_status(db, user: User, day: date, allowed_misses: int = 1) -> dict:
    required_events = _count_required_events(db, user, day)
    required_total = _required_daily_count() + required_events

    completed_daily = _count_completed_daily(db, user, day)
    completed_events = _count_completed_event_photos(db, user, day)
    completed_total = completed_daily + completed_events

    misses = max(0, required_total - completed_total)
    honored = misses <= allowed_misses

    return {
        "day": day.isoformat(),
        "required_events": required_events,
        "required_total": required_total,
        "completed_daily": completed_daily,
        "completed_event_photos": completed_events,
        "completed_total": completed_total,
        "misses": misses,
        "allowed_misses": allowed_misses,
        "honored": honored,
    }

def compute_streak(db, user: User, end_day: date, allowed_misses: int = 1) -> tuple[int, int]:
    """
    (current_streak_ending_end_day, best_streak_over_last_365_days_scan)
    """
    best = 0
    cur = 0

    d = end_day
    for i in range(365):
        st = compute_day_status(db, user, d, allowed_misses=allowed_misses)
        if st["honored"]:
            cur += 1
            best = max(best, cur)
        else:
            best = max(best, cur)
            if i == 0:
                cur = 0
                break
            cur = 0
        d = d - timedelta(days=1)

    return cur, best

def format_status_line(st: dict) -> str:
    misses_left = max(0, st["allowed_misses"] - st["misses"])
    return (
        f"Status: {st['completed_total']}/{st['required_total']} done "
        f"(misses {st['misses']}, left {misses_left}). "
        f"Honored today: {'YES' if st['honored'] else 'NO'}"
    )
