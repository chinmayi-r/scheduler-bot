from __future__ import annotations

import json
from datetime import date

from ..db import MealLog, User

DEFAULT_MEAL_TIMES = {"breakfast": "08:30", "lunch": "13:00", "dinner": "19:00"}


def load_meal_times(user: User) -> dict[str, str]:
    try:
        d = json.loads(user.meal_times_json or "{}")
        if isinstance(d, dict) and d:
            return d
    except Exception:
        pass
    return dict(DEFAULT_MEAL_TIMES)


def save_meal_times(db, user: User, meal_times: dict[str, str]) -> None:
    user.meal_times_json = json.dumps(meal_times)
    db.commit()


def log_meal(db, user: User, day: date, meal: str, status: str, note: str | None = None,
             photo_file_id: str | None = None) -> MealLog:
    row = (
        db.query(MealLog)
        .filter(MealLog.user_id == user.id, MealLog.day == day, MealLog.meal == meal)
        .one_or_none()
    )
    if row is None:
        row = MealLog(user_id=user.id, day=day, meal=meal, status=status)
        db.add(row)
    row.status = status
    if note:
        row.note = note
    if photo_file_id:
        row.photo_file_id = photo_file_id
    db.commit()
    db.refresh(row)
    return row


def today_meal_status(db, user: User, day: date) -> dict[str, str]:
    rows = db.query(MealLog).filter(MealLog.user_id == user.id, MealLog.day == day).all()
    return {r.meal: r.status for r in rows}


def format_meal_summary(user: User, statuses: dict[str, str]) -> str:
    meal_times = load_meal_times(user)
    if not meal_times:
        return "No meals configured."
    lines = []
    for meal in meal_times:
        st = statuses.get(meal)
        icon = "✅" if st == "logged" else ("⏭️" if st == "skipped" else "⬜")
        lines.append(f"{icon} {meal.capitalize()}")
    return "\n".join(lines)
