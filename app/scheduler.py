from __future__ import annotations

import os
import json
from datetime import datetime, timedelta

import pytz
from telegram.ext import ContextTypes

from .db import SessionLocal, User, TodoCache, DailyEventIndex, Person, Checkin
from .services.formatters import format_events, format_people, format_todoist_tasks_numbered
from .services.todoist import list_active_tasks as todoist_list_tasks, TodoistError, default_project_id
from .services.timeutil import today_in_tz
from .commands import _build_daily_event_index

TEST_TRIGGERS = {}  # chat_id -> dict of label -> datetime

def _load_meal_times() -> dict[str, str]:
    raw = os.environ.get("MEAL_TIMES_JSON", "").strip()
    if not raw:
        # defaults
        return {"breakfast":"08:30","fruit":"12:00","lunch":"14:00","dinner":"19:00"}
    return json.loads(raw)

MEAL_TIMES = _load_meal_times()         # name -> "HH:MM"
MEALS = {v: k for k, v in MEAL_TIMES.items()}  # "HH:MM" -> name


def start_scheduler(app) -> None:
    app.job_queue.run_repeating(tick, interval=60, first=1)


async def _send(app, chat_id: str, text: str) -> None:
    await app.bot.send_message(chat_id=int(chat_id), text=text)


def _utc_now() -> datetime:
    return datetime.utcnow()


def _now_local(tz_name: str) -> datetime:
    tz = pytz.timezone(tz_name)
    return pytz.utc.localize(_utc_now()).astimezone(tz)


def _same_minute(a: datetime, b: datetime) -> bool:
    return a.strftime("%Y-%m-%d %H:%M") == b.strftime("%Y-%m-%d %H:%M")


async def _maybe_fire_meal_checkin(app, db, u: User, now_local: datetime) -> None:
    hhmm = now_local.strftime("%H:%M")
    if hhmm not in MEALS:
        return

    day = today_in_tz(u.timezone)
    meal = MEALS[hhmm]

    # only once per day per meal
    existing = (
        db.query(Checkin)
        .filter(Checkin.user_id == u.id, Checkin.day == day, Checkin.kind == "meal", Checkin.ref == meal)
        .one_or_none()
    )
    if existing:
        return

    msg = f"{meal.capitalize()} check-in. What did you have? Send a pic if you want."
    await _send(app, u.telegram_chat_id, msg)

    db.add(Checkin(user_id=u.id, day=day, kind="meal", ref=meal, prompted_at=_utc_now()))
    db.commit()


async def _maybe_fire_event_checkins(app, db, u: User, now_local: datetime) -> None:
    """
    For each event in today's index, prompt at start+5min (local),
    once per event occurrence.
    """
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
        # normalize start_dt to aware UTC
        start = ev.start_dt
        if start.tzinfo is None:
            start_utc = pytz.utc.localize(start)
        else:
            start_utc = start.astimezone(pytz.utc)

        start_local = start_utc.astimezone(tz)
        fire_local = start_local + timedelta(minutes=5)

        if not _same_minute(now_local, fire_local):
            continue

        # already prompted?
        existing = (
            db.query(Checkin)
            .filter(Checkin.user_id == u.id, Checkin.day == day, Checkin.kind == "event", Checkin.ref == ev.google_event_id)
            .one_or_none()
        )
        if existing:
            continue

        await _send(app, u.telegram_chat_id, f"Check-in: {ev.event_number}) {ev.title}\nHow’s it going? Send a pic.")
        db.add(Checkin(user_id=u.id, day=day, kind="event", ref=ev.google_event_id, prompted_at=_utc_now()))
        db.commit()


async def tick(context: ContextTypes.DEFAULT_TYPE) -> None:
    app = context.application
    test_mode = os.environ.get("TEST_SCHEDULE", "0") == "1"

    db = SessionLocal()
    try:
        users = db.query(User).all()
        for u in users:
            now_local = _now_local(u.timezone)
            hhmm = now_local.strftime("%H:%M")

            # ---- TEST MODE: fire 7:00/7:15/7:30/21:00 in next minutes ----
            if test_mode:
                triggers = TEST_TRIGGERS.setdefault(u.telegram_chat_id, {
                    "07:00": now_local + timedelta(minutes=1),
                    "07:15": now_local + timedelta(minutes=2),
                    "07:30": now_local + timedelta(minutes=3),
                    "21:00": now_local + timedelta(minutes=4),
                    "_fired": set(),
                })
                fired = triggers["_fired"]

                def should_fire(label: str) -> bool:
                    t = triggers[label]
                    return (
                        label not in fired
                        and now_local.strftime("%Y-%m-%d %H:%M") == t.strftime("%Y-%m-%d %H:%M")
                    )

                if should_fire("07:00"):
                    fired.add("07:00")
                    await _send(app, u.telegram_chat_id, "TEST 07:00 ...")

                if should_fire("07:15"):
                    fired.add("07:15")
                    await _send(app, u.telegram_chat_id, "TEST 07:15 ...")

                if should_fire("07:30"):
                    fired.add("07:30")
                    await _send(app, u.telegram_chat_id, "TEST 07:30 ...")

                if should_fire("21:00"):
                    fired.add("21:00")
                    await _send(app, u.telegram_chat_id, "TEST 21:00 ...")

                continue  # <-- IMPORTANT: do not run normal mode below


            # ---- NORMAL MODE ----

            # If the user requested refresh earlier, reschedule within 60 seconds.
            if u.needs_reschedule:
                # rebuild index + clear flag
                _build_daily_event_index(u)
                u.needs_reschedule = False
                db.commit()

            if should_fire("07:00"):
                fired.add("07:00")
                people = db.query(Person).filter(Person.user_id == u.id).all()
                people_msg = format_people(people, u.timezone)

                try:
                    tasks = todoist_list_tasks(project_id=default_project_id())
                    todos_msg = format_todoist_tasks_numbered(tasks)
                except TodoistError as e:
                    todos_msg = f"(Todoist error: {e})"

                msg = (
                    "Morning! Please set up today’s calendar.\n\n"
                    "Todos:\n" + todos_msg + "\n\n"
                    "People:\n" + people_msg
                )
                await _send(app, u.telegram_chat_id, msg)

            if hhmm == "07:15":
                _build_daily_event_index(u)
                day = today_in_tz(u.timezone)
                events = db.query(DailyEventIndex).filter(DailyEventIndex.user_id == u.id, DailyEventIndex.day == day).all()
                await _send(app, u.telegram_chat_id, "Today’s events:\n" + format_events(events, u.timezone))

            if hhmm == "07:30":
                await _send(app, u.telegram_chat_id, "Running time! Shoes on. Reply when you’re back.")

            if hhmm == "21:00":
                await _send(app, u.telegram_chat_id, "Wind-down: 2 min brain dump + pick tomorrow’s TODOs.")

            # meals + per-event checkins
            await _maybe_fire_meal_checkin(app, db, u, now_local)
            await _maybe_fire_event_checkins(app, db, u, now_local)

    finally:
        db.close()
