from __future__ import annotations

import json
from datetime import timedelta, date

from ..config import STREAK_BREAK_AFTER_MISSES
from ..db import DailyLog, User


def _get_log(db, user: User, day: date) -> DailyLog | None:
    return db.query(DailyLog).filter(DailyLog.user_id == user.id, DailyLog.day == day).one_or_none()


def get_or_create_log(db, user: User, day: date) -> DailyLog:
    row = _get_log(db, user, day)
    if row is None:
        row = DailyLog(user_id=user.id, day=day)
        db.add(row)
        db.commit()
        db.refresh(row)
    return row


def is_engaged(db, user: User, day: date) -> bool:
    """
    A day 'counts' if you showed up at all: responded to the morning plan,
    completed at least one task, or did the wind-down. Deliberately generous —
    the point is showing up, not perfection.
    """
    row = _get_log(db, user, day)
    if row is None:
        return False
    if row.morning_responded_at is not None or row.evening_responded_at is not None:
        return True
    try:
        completed = json.loads(row.completed_task_ids_json or "[]")
    except Exception:
        completed = []
    return len(completed) > 0


def current_streak(db, user: User, today: date, lookback_days: int = 365) -> int:
    """
    Counts consecutive engaged days ending yesterday (today is still in
    progress, so it isn't judged yet). STREAK_BREAK_AFTER_MISSES controls how
    forgiving this is -- at the default of 2 it's "never miss twice": one slip
    doesn't zero the streak, two in a row does.
    """
    tolerance = max(1, STREAK_BREAK_AFTER_MISSES)
    cur = 0
    miss_run = 0
    d = today - timedelta(days=1)
    for _ in range(lookback_days):
        if is_engaged(db, user, d):
            cur += 1
            miss_run = 0
        else:
            miss_run += 1
            if miss_run >= tolerance:
                break
        d -= timedelta(days=1)
    return cur


def days_since_last_engagement(db, user: User, today: date, lookback_days: int = 60) -> int | None:
    """How long you've been away. None means no engagement in the whole window,
    which reads as 'never/long gone' rather than 'yesterday'."""
    d = today - timedelta(days=1)
    for gap in range(1, lookback_days + 1):
        if is_engaged(db, user, d):
            return gap
        d -= timedelta(days=1)
    return None


def engaged_last_n_days(db, user: User, today: date, n: int = 30) -> int:
    count = 0
    d = today - timedelta(days=1)
    for _ in range(n):
        if is_engaged(db, user, d):
            count += 1
        d -= timedelta(days=1)
    return count


def format_streak_line(db, user: User, today: date) -> str:
    streak = current_streak(db, user, today)
    engaged_30 = engaged_last_n_days(db, user, today, 30)
    if streak == 0:
        return f"🔥 Day 0 — let's start today. ({engaged_30}/30 days engaged this month)"
    tol = max(1, STREAK_BREAK_AFTER_MISSES)
    rule = ("any missed day resets it" if tol == 1
            else f"{tol} missed days in a row resets the count (not you)")
    return f"🔥 Day {streak} — {rule}. ({engaged_30}/30 this month)"
