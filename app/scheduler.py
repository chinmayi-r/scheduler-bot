from __future__ import annotations

import os
import json
from datetime import datetime, timedelta
from typing import Dict

import pytz
from telegram.ext import ContextTypes

from .db import SessionLocal, User, Person, DailyEventIndex, Checkin
from .config import MEAL_TIMES_JSON, TEST_SCHEDULE, ALLOWED_MISSES_PER_DAY 
from .services.formatters import format_events, format_people, format_todoist_tasks_numbered
from .services.todoist import list_active_tasks as todoist_list_tasks, TodoistError, default_project_id
from .services.streaks import compute_day_status, compute_streak, format_status_line
from .services.timeutil import today_in_tz
from .commands import _build_daily_event_index

# chat_id -> dict(label->datetime) used only in TEST mode
TEST_TRIGGERS: dict[str, dict] = {}


# Time helpers

def _utc_now() -> datetime:
    # naive UTC; we always localize when needed
    return datetime.utcnow()


def _now_local(tz_name: str) -> datetime:
    tz = pytz.timezone(tz_name)
    return pytz.utc.localize(_utc_now()).astimezone(tz)


def _same_minute(a: datetime, b: datetime) -> bool:
    return a.strftime("%Y-%m-%d %H:%M") == b.strftime("%Y-%m-%d %H:%M")


async def _send(app, chat_id: str, text: str) -> None:
    await app.bot.send_message(chat_id=int(chat_id), text=text)


# Meal times

def _load_meal_times() -> Dict[str, str]:
    """
    Load MEAL_TIMES_JSON={"breakfast":"08:30","fruit":"12:00","lunch":"14:00","dinner":"19:00"}
    Returns name->"HH:MM".
    Robust: defaults if missing/bad JSON.
    """
    defaults = {"breakfast": "08:30", "fruit": "12:00", "lunch": "14:00", "dinner": "19:00"}

    raw = MEAL_TIMES_JSON
    if not raw:
        return defaults

    try:
        d = json.loads(raw)
        if not isinstance(d, dict):
            return defaults
        out = {}
        for k, v in d.items():
            kk = str(k).strip().lower()
            vv = str(v).strip()
            # Basic HH:MM validation
            if len(vv) == 5 and vv[2] == ":" and vv[:2].isdigit() and vv[3:].isdigit():
                out[kk] = vv
        return out or defaults
    except Exception:
        return defaults


def _meal_lookup(meal_times: Dict[str, str]) -> Dict[str, str]:
    """
    Invert name->HH:MM into HH:MM->name
    """
    inv = {}
    for name, hhmm in meal_times.items():
        inv[hhmm] = name
    return inv


# Dedupe gate (important)

def _checkin_exists(db, user_id: int, day, kind: str, ref: str) -> bool:
    return (
        db.query(Checkin)
        .filter(Checkin.user_id == user_id, Checkin.day == day, Checkin.kind == kind, Checkin.ref == ref)
        .one_or_none()
        is not None
    )


def _mark_checkin(db, user_id: int, day, kind: str, ref: str) -> None:
    db.add(Checkin(user_id=user_id, day=day, kind=kind, ref=ref, prompted_at=_utc_now()))
    db.commit()


# Daily prompts

async def _maybe_fire_daily_prompts(app, db, u: User, now_local: datetime) -> None:
    """
    Fires the 4 main prompts once per day:
    07:00 morning
    07:15 events list
    07:30 running
    21:00 wind-down
    Uses Checkin(kind="daily", ref=...) for dedupe.
    """
    hhmm = now_local.strftime("%H:%M")
    day = today_in_tz(u.timezone)

    if hhmm == "07:00":
        if not _checkin_exists(db, u.id, day, "daily", "morning"):
            people = db.query(Person).filter(Person.user_id == u.id).all()
            people_msg = format_people(people, u.timezone)

            try:
                tasks = todoist_list_tasks(project_id=default_project_id())
                todos_msg = format_todoist_tasks_numbered(tasks)
            except TodoistError as e:
                todos_msg = f"(Todoist error: {e})"

            day = today_in_tz(u.timezone)
            st = compute_day_status(db, u, day, allowed_misses=ALLOWED_MISSES_PER_DAY)
            cur, best = compute_streak(db, u, day, allowed_misses=ALLOWED_MISSES_PER_DAY)
            status_line = format_status_line(st)
            streak_line = f"Streak: {cur} day(s) (best {best})"

            msg = (
                "Morning! Please set up today’s calendar.\n\n"
                f"{status_line}\n{streak_line}\n\n"
                "Todos:\n" + todos_msg + "\n\n"
                "People:\n" + people_msg
            )
            await _send(app, u.telegram_chat_id, msg)
            _mark_checkin(db, u.id, day, "daily", "morning")

    elif hhmm == "07:15":
        if not _checkin_exists(db, u.id, day, "daily", "events_list"):
            _build_daily_event_index(u)
            events = (
                db.query(DailyEventIndex)
                .filter(DailyEventIndex.user_id == u.id, DailyEventIndex.day == day)
                .all()
            )
            await _send(app, u.telegram_chat_id, "Today’s events:\n" + format_events(events, u.timezone))
            _mark_checkin(db, u.id, day, "daily", "events_list")

    elif hhmm == "07:30":
        if not _checkin_exists(db, u.id, day, "daily", "run"):
            await _send(app, u.telegram_chat_id, "Running time! Shoes on. Reply when you’re back.")
            _mark_checkin(db, u.id, day, "daily", "run")

    elif hhmm == "21:00":
        if not _checkin_exists(db, u.id, day, "daily", "winddown"):
            day = today_in_tz(u.timezone)
            st = compute_day_status(db, u, day, allowed_misses=ALLOWED_MISSES_PER_DAY)
            cur, best = compute_streak(db, u, day, allowed_misses=ALLOWED_MISSES_PER_DAY)
            status_line = format_status_line(st)
            streak_line = f"Streak: {cur} day(s) (best {best})"
            await _send(
                app,
                u.telegram_chat_id,
                "Wind-down: 2 min brain dump + pick tomorrow’s TODOs.\n\n"
                f"{status_line}\n{streak_line}"
            )
            _mark_checkin(db, u.id, day, "daily", "winddown")


# Meal prompts

async def _maybe_fire_meal_checkins(app, db, u: User, now_local: datetime, meal_by_time: Dict[str, str]) -> None:
    hhmm = now_local.strftime("%H:%M")
    if hhmm not in meal_by_time:
        return

    day = today_in_tz(u.timezone)
    meal = meal_by_time[hhmm]  # e.g. "breakfast"

    if _checkin_exists(db, u.id, day, "meal", meal):
        return

    await _send(app, u.telegram_chat_id, f"{meal.capitalize()} check-in. What did you have? Send a pic if you want.")
    _mark_checkin(db, u.id, day, "meal", meal)


# Event photo check-ins (start + 5 min)

async def _maybe_fire_event_checkins(app, db, u: User, now_local: datetime) -> None:
    day = today_in_tz(u.timezone)

    events = (
        db.query(DailyEventIndex)
        .filter(DailyEventIndex.user_id == u.id, DailyEventIndex.day == day)
        .all()
    )
    if not events:
        return

    tz = pytz.timezone(u.timezone)

    for ev in events:
        start = ev.start_dt
        if start.tzinfo is None:
            start_utc = pytz.utc.localize(start)
        else:
            start_utc = start.astimezone(pytz.utc)

        fire_local = start_utc.astimezone(tz) + timedelta(minutes=5)

        if not _same_minute(now_local, fire_local):
            continue

        # Dedupe per event occurrence
        ref = ev.google_event_id
        if _checkin_exists(db, u.id, day, "event", ref):
            continue

        await _send(
            app,
            u.telegram_chat_id,
            f"Check-in: {ev.event_number}) {ev.title}\nHow’s it going? Send a pic."
        )
        _mark_checkin(db, u.id, day, "event", ref)


# TEST mode (fast-fire)

async def _maybe_fire_test_prompts(app, db, u: User, now_local: datetime) -> None:
    """
    Fires the 4 daily prompts within the next minutes, once.
    Uses in-memory TEST_TRIGGERS only.
    """
    key = u.telegram_chat_id
    if key not in TEST_TRIGGERS:
        TEST_TRIGGERS[key] = {
            "07:00": now_local + timedelta(minutes=1),
            "07:15": now_local + timedelta(minutes=2),
            "07:30": now_local + timedelta(minutes=3),
            "21:00": now_local + timedelta(minutes=4),
            "_fired": set(),
        }

    triggers = TEST_TRIGGERS[key]
    fired = triggers["_fired"]

    def should_fire(label: str) -> bool:
        t = triggers[label]
        return label not in fired and _same_minute(now_local, t)

    if should_fire("07:00"):
        fired.add("07:00")
        await _send(app, u.telegram_chat_id, "TEST 07:00 (morning)")

    if should_fire("07:15"):
        fired.add("07:15")
        await _send(app, u.telegram_chat_id, "TEST 07:15 (events list)")

    if should_fire("07:30"):
        fired.add("07:30")
        await _send(app, u.telegram_chat_id, "TEST 07:30 (run)")

    if should_fire("21:00"):
        fired.add("21:00")
        await _send(app, u.telegram_chat_id, "TEST 21:00 (wind-down)")


# Entry points

def start_scheduler(app) -> None:
    app.job_queue.run_repeating(tick, interval=60, first=1)


async def tick(context: ContextTypes.DEFAULT_TYPE) -> None:
    app = context.application
    test_mode = (TEST_SCHEDULE == "1")

    meal_times = _load_meal_times()
    meal_by_time = _meal_lookup(meal_times)

    db = SessionLocal()
    try:
        users = db.query(User).all()

        for u in users:
            now_local = _now_local(u.timezone)

            # TEST MODE
            if test_mode:
                await _maybe_fire_test_prompts(app, db, u, now_local)
                # still allow meal + event checkins in test mode if you want;
                # comment these out if you only want the 4 test pings
                await _maybe_fire_meal_checkins(app, db, u, now_local, meal_by_time)
                await _maybe_fire_event_checkins(app, db, u, now_local)
                continue

            # NORMAL MODE

            # If refresh requested (set by commands), rebuild index now (and clear flag).
            if getattr(u, "needs_reschedule", False):
                _build_daily_event_index(u)
                u.needs_reschedule = False
                db.commit()

            await _maybe_fire_daily_prompts(app, db, u, now_local)
            await _maybe_fire_meal_checkins(app, db, u, now_local, meal_by_time)
            await _maybe_fire_event_checkins(app, db, u, now_local)

    finally:
        db.close()
