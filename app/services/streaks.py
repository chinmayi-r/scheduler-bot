from __future__ import annotations

from datetime import timedelta, date

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
    return (
        db.query(Checkin)
        .filter(
            Checkin.user_id == user.id,
            Checkin.day == day,
            Checkin.kind == "event",
            Checkin.responded_at.is_not(None),
        )
        .filter((Checkin.photo_file_id.is_not(None)) | (Checkin.response_text.is_not(None)))
        .count()
    )


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
        "required_daily": _required_daily_count(),
        "required_events": required_events,
        "required_total": required_total,
        "completed_daily": completed_daily,
        "completed_event_photos": completed_events,
        "completed_total": completed_total,
        "misses": misses,
        "allowed_misses": allowed_misses,
        "honored": honored,
    }


def compute_streak(db, user: User, end_day: date, allowed_misses: int = 1) -> tuple[int, int, bool]:
    """
    Returns (current_streak, best_streak_over_365d, today_in_progress).

    current_streak: consecutive honored days ending at end_day (today).
                    If today is not yet honored, it counts up through yesterday —
                    the streak is not broken, it's just not closed yet.
    best_streak:    longest consecutive honored-day run in the last 365 days.
    today_in_progress: True when today is not honored but yesterday continues the streak.
    """
    best = 0
    run = 0
    current_streak = None  # set when the current run ends (first miss going back)
    today_honored = None

    for i in range(365):
        d = end_day - timedelta(days=i)
        st = compute_day_status(db, user, d, allowed_misses=allowed_misses)

        if i == 0:
            today_honored = st["honored"]
            if not today_honored:
                # Today is incomplete/missed — skip it for streak counting.
                # current_streak will be set from the days before today.
                continue

        if st["honored"]:
            run += 1
            best = max(best, run)
        else:
            # Miss found: freeze current_streak if we haven't yet
            if current_streak is None:
                current_streak = run
            run = 0

    if current_streak is None:
        current_streak = run

    today_in_progress = (not today_honored) and (current_streak > 0)
    return current_streak, best, today_in_progress


def format_status_line(st: dict) -> str:
    icon = "✅" if st["honored"] else "⏳"
    misses_left = max(0, st["allowed_misses"] - st["misses"])
    return (
        f"{icon} {st['completed_total']}/{st['required_total']} done "
        f"(misses: {st['misses']}, {misses_left} left)"
    )


def format_streak_line(cur: int, best: int, today_in_progress: bool) -> str:
    if today_in_progress:
        return f"🔥 {cur}d streak (today in progress) | best {best}d"
    elif cur > 0:
        return f"🔥 {cur}d streak | best {best}d"
    else:
        return f"No active streak | best {best}d"
