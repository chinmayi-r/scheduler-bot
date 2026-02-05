from __future__ import annotations

import re
from datetime import datetime

from .db import SessionLocal, User, Person, DailyEventIndex, EventDone, Note
from .config import DEFAULT_TIMEZONE
from .services.timeutil import today_in_tz, is_after_local_hour
from .services.gcal import fetch_events_for_day_multi_ics
from .services.todoist import (
    add_task as todoist_add_task,
    list_active_tasks as todoist_list_tasks,
    close_task as todoist_close_task,
    TodoistError,
    default_project_id,
)
from .services.formatters import (
    format_people,
    format_events,
    format_todoist_tasks_numbered,
)

# Helpers

def get_or_create_user(telegram_chat_id: str) -> User:
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.telegram_chat_id == telegram_chat_id).one_or_none()
        if user is None:
            user = User(telegram_chat_id=telegram_chat_id, timezone=DEFAULT_TIMEZONE)
            db.add(user)
            db.commit()
            db.refresh(user)
        return user
    finally:
        db.close()

# Regex

# People
PERSON_ADD_RE = re.compile(
    r"^\s*PERSON\s+ADD\s*->\s*(.+?)\s*\|\s*p\s*=\s*(\d+)\s*\|\s*note\s*=\s*(.+?)\s*(?:\|\s*days\s*=\s*(-?\d+)\s*)?$",
    re.IGNORECASE
)
PERSON_LIST_RE = re.compile(r"^\s*PERSON\s+LIST\s*$", re.IGNORECASE)
PERSON_DEL_RE  = re.compile(r"^\s*PERSON\s+DEL\s*->\s*(.+?)\s*$", re.IGNORECASE)
PERSON_QUICK_RE = re.compile(r"^\s*PERSON\s*->\s*([^,]+?)\s*,\s*(-?\d+)\s*$", re.IGNORECASE)

# Events
EVENTS_TODAY_RE = re.compile(r"^\s*EVENTS\s+TODAY\s*$", re.IGNORECASE)
EVENTS_REFRESH_RE = re.compile(r"^\s*EVENTS\s+REFRESH\s*$", re.IGNORECASE)

# Todoist tasks
TODO_ADD_RE = re.compile(
    r"^\s*TODO\s+ADD\s*->\s*(.+?)(?:\s*\|\s*due\s*=\s*(.+))?\s*$",
    re.IGNORECASE
)
TODO_LIST_RE = re.compile(r"^\s*TODO\s+LIST\s*$", re.IGNORECASE)
DONE_TODO_RE = re.compile(r"^\s*DONE\s+TODO\s*->\s*([0-9,\s]+)\s*$", re.IGNORECASE)

# Timezone
TZ_SET_RE = re.compile(r"^\s*TZ\s*->\s*([A-Za-z_]+\/[A-Za-z_]+)\s*$", re.IGNORECASE)

# ACMD-lite
ACMD_RE = re.compile(r"^\s*ACMD\s*=\s*(.+?)\s*$", re.IGNORECASE)

# Cache: chat_id -> last TODO LIST tasks
TODO_LIST_CACHE: dict[str, list] = {}

# Help text

def help_text() -> str:
    return (
        "Commands:\n"
        "PERSON-> name, days\n"
        "PERSON ADD -> name | p=8 | note=one line | days=2\n"
        "PERSON LIST\n"
        "PERSON DEL -> name\n"
        "EVENTS TODAY\n"
        "EVENTS REFRESH   (after 4pm)\n"
        "TODO ADD -> task\n"
        "TODO ADD -> task | due=Thursday\n"
        "TODO ADD -> task | due=tomorrow 5pm\n"
        "TODO ADD -> task | due=every Sunday 6pm   (recurring)\n"
        "TODO LIST\n"
        "DONE TODO -> 1,2,3   (requires TODO LIST first)\n"
        "TZ-> Area/City\n\n"
        "ACMD-lite shortcuts:\n"
        "ACMD=events\n"
        "ACMD=refresh\n"
        "ACMD=todo buy milk\n"
        "ACMD=todo buy milk due thursday\n"
        "ACMD=todo laundry due every sunday 6pm\n"
        "ACMD=done todo 1 2 3\n"
        "ACMD=person john 2\n"
        "ACMD=tz America/New_York\n\n"
        "Notes:\n"
        "Anything else you send is saved as a daily note."
    )

# ACMD mapping

def _acmd_to_command(acmd: str) -> str | None:
    """
    Convert ACMD-lite natural-ish phrases into strict commands.
    Deterministic + safe.
    """
    s = acmd.strip()
    low = s.lower()

    if low in {"events", "today", "calendar"}:
        return "EVENTS TODAY"

    if low in {"refresh", "re-read", "reread"}:
        return "EVENTS REFRESH"

    # ACMD=todo <task> [due <due-string>]
    m = re.match(r"^\s*todo\s+(.+?)\s*(?:\s+due\s+(.+))?\s*$", s, flags=re.IGNORECASE)
    if m:
        task = m.group(1).strip()
        due = (m.group(2) or "").strip()
        if due:
            return f"TODO ADD -> {task} | due={due}"
        return f"TODO ADD -> {task}"

    # ACMD=done todo 1 2 3
    m = re.match(r"^\s*done\s+todo\s+([0-9,\s]+)\s*$", s, flags=re.IGNORECASE)
    if m:
        nums = ",".join([p.strip() for p in m.group(1).replace(" ", ",").split(",") if p.strip()])
        return f"DONE TODO -> {nums}"

    # ACMD=person <name> <days>
    m = re.match(r"^\s*person\s+(.+?)\s+(-?\d+)\s*$", s, flags=re.IGNORECASE)
    if m:
        name = m.group(1).strip()
        days = m.group(2).strip()
        return f"PERSON-> {name}, {days}"

    # ACMD=tz Area/City
    m = re.match(r"^\s*tz\s+([A-Za-z_]+\/[A-Za-z_]+)\s*$", s, flags=re.IGNORECASE)
    if m:
        return f"TZ-> {m.group(1)}"

    return None

# Main command handler

def handle_text_command(chat_id: str, text: str) -> str:
    user = get_or_create_user(chat_id)
    raw = text or ""
    raw_stripped = raw.strip()

    # ACMD-lite
    m = ACMD_RE.match(raw)
    if m:
        cmd = _acmd_to_command(m.group(1))
        if not cmd:
            return (
                "ACMD not recognized.\n\n"
                "Try:\n"
                "ACMD=events\n"
                "ACMD=refresh\n"
                "ACMD=todo <text>\n"
                "ACMD=todo <text> due <when>\n"
                "ACMD=done todo 1 2\n"
                "ACMD=person <name> <days>\n"
                "ACMD=tz Area/City"
            )
        return handle_text_command(chat_id, cmd)

    # Help keyword
    if raw_stripped.upper() in {"HELP", "COMMANDS"}:
        return help_text()

    # TZ change
    m = TZ_SET_RE.match(raw)
    if m:
        tz = m.group(1)
        db = SessionLocal()
        try:
            u = db.query(User).filter(User.telegram_chat_id == chat_id).one()
            u.timezone = tz
            db.commit()
            return f"✅ Timezone set to {tz}."
        finally:
            db.close()

    # PERSON-> name, days
    m = PERSON_QUICK_RE.match(raw)
    if m:
        name = m.group(1).strip()
        base_days = int(m.group(2))
        today = today_in_tz(user.timezone)

        db = SessionLocal()
        try:
            existing = db.query(Person).filter(Person.user_id == user.id, Person.name.ilike(name)).one_or_none()
            if existing:
                existing.name = name
                existing.base_days = base_days
                existing.start_day = today
                existing.updated_at = datetime.utcnow()
                db.commit()
                return f"✅ Tracking updated: {name}, {base_days} days (start={today.isoformat()})"
            else:
                p = Person(
                    user_id=user.id,
                    name=name,
                    priority=5,
                    note="(quick add)",
                    base_days=base_days,
                    start_day=today,
                )
                db.add(p)
                db.commit()
                return f"✅ Tracking started: {name}, {base_days} days (start={today.isoformat()})"
        finally:
            db.close()

    # PERSON ADD -> ... | days= optional
    m = PERSON_ADD_RE.match(raw)
    if m:
        name = m.group(1).strip()
        prio = int(m.group(2))
        note = m.group(3).strip()
        days_str = m.group(4)
        base_days = int(days_str) if days_str is not None else None
        today = today_in_tz(user.timezone)

        if not (1 <= prio <= 10):
            return "Priority must be 1..10."

        db = SessionLocal()
        try:
            existing = db.query(Person).filter(Person.user_id == user.id, Person.name.ilike(name)).one_or_none()
            if existing:
                existing.name = name
                existing.priority = prio
                existing.note = note
                if base_days is not None:
                    existing.base_days = base_days
                    existing.start_day = today
                existing.updated_at = datetime.utcnow()
                db.commit()
                return f"✅ Updated: {name} (P{prio})"
            else:
                p = Person(
                    user_id=user.id,
                    name=name,
                    priority=prio,
                    note=note,
                    base_days=base_days,
                    start_day=today if base_days is not None else None,
                )
                db.add(p)
                db.commit()
                return f"✅ Added: {name} (P{prio})"
        finally:
            db.close()

    # PERSON LIST
    if PERSON_LIST_RE.match(raw):
        db = SessionLocal()
        try:
            people = db.query(Person).filter(Person.user_id == user.id).all()
            return format_people(people, user.timezone)
        finally:
            db.close()

    # PERSON DEL
    m = PERSON_DEL_RE.match(raw)
    if m:
        name = m.group(1).strip()
        db = SessionLocal()
        try:
            obj = db.query(Person).filter(Person.user_id == user.id, Person.name.ilike(name)).one_or_none()
            if not obj:
                return f"Couldn't find person: {name}"
            db.delete(obj)
            db.commit()
            return f"🗑️ Deleted: {obj.name}"
        finally:
            db.close()

    # EVENTS TODAY
    if EVENTS_TODAY_RE.match(raw):
        return _events_today(user)

    # EVENTS REFRESH (after 4pm local)
    if EVENTS_REFRESH_RE.match(raw):
        if not is_after_local_hour(user.timezone, 16):
            return "EVENTS REFRESH is only available after 4pm local time."

        _build_daily_event_index(user)

        # If you have needs_reschedule on User, keep this:
        db = SessionLocal()
        try:
            u = db.query(User).filter(User.id == user.id).one()
            if hasattr(u, "needs_reschedule"):
                u.needs_reschedule = True
            db.commit()
        finally:
            db.close()

        return "✅ Refreshed today’s events.\n\n" + _events_today(user)

    # TODO ADD (Todoist)
    m = TODO_ADD_RE.match(raw)
    if m:
        todo_text = m.group(1).strip()
        due = m.group(2).strip() if m.group(2) else None
        if not todo_text:
            return "TODO text can’t be empty."

        try:
            todoist_add_task(
                todo_text,
                project_id=default_project_id(),
                due_string=due,
            )
            if due:
                return f"✅ Added Todoist task: {todo_text} (due {due})"
            return f"✅ Added Todoist task: {todo_text}"
        except TodoistError as e:
            return f"Todoist error: {e}"

    # TODO LIST (fills cache for DONE TODO)
    if TODO_LIST_RE.match(raw):
        try:
            tasks = todoist_list_tasks(project_id=default_project_id())
            TODO_LIST_CACHE[chat_id] = tasks
            return format_todoist_tasks_numbered(tasks)
        except TodoistError as e:
            return f"Todoist error: {e}"

    # DONE TODO -> 1,2,3  (uses last TODO LIST cache)
    m = DONE_TODO_RE.match(raw)
    if m:
        nums_raw = m.group(1)
        nums: list[int] = []
        for part in nums_raw.replace(" ", ",").split(","):
            part = part.strip()
            if not part:
                continue
            try:
                nums.append(int(part))
            except ValueError:
                return "Invalid numbers. Example: DONE TODO -> 1,2,3"

        tasks = TODO_LIST_CACHE.get(chat_id, [])
        if not tasks:
            return "Run TODO LIST first so I know which task numbers you mean."

        missing = [n for n in nums if n < 1 or n > len(tasks)]
        if missing:
            return f"These task numbers don’t exist: {missing}\nRun TODO LIST again."

        closed_lines = []
        try:
            for n in nums:
                t = tasks[n - 1]
                todoist_close_task(t.id)
                closed_lines.append(f"- {n}) {t.content}")
        except TodoistError as e:
            return f"Todoist error: {e}"

        return "✅ Completed:\n" + "\n".join(closed_lines)

    # Otherwise: treat as a daily note
    if raw_stripped:
        db = SessionLocal()
        try:
            db.add(Note(user_id=user.id, day=today_in_tz(user.timezone), text=raw_stripped))
            db.commit()
        finally:
            db.close()
        return "📝 Saved."

    return help_text()

# Event indexing helpers

def _build_daily_event_index(user: User) -> None:
    day = today_in_tz(user.timezone)
    events = fetch_events_for_day_multi_ics(user.timezone, day)

    db = SessionLocal()
    try:
        db.query(DailyEventIndex).filter(DailyEventIndex.user_id == user.id, DailyEventIndex.day == day).delete()

        events_sorted = sorted(events, key=lambda e: e.start_utc)
        for i, ev in enumerate(events_sorted, start=1):
            db.add(DailyEventIndex(
                user_id=user.id,
                day=day,
                event_number=i,
                google_event_id=ev.event_id,
                title=f"{ev.title} [{ev.source}]",
                start_dt=ev.start_utc,
                end_dt=ev.end_utc,
            ))
        db.commit()
    finally:
        db.close()

def _events_today(user: User) -> str:
    day = today_in_tz(user.timezone)
    db = SessionLocal()
    try:
        rows = db.query(DailyEventIndex).filter(
            DailyEventIndex.user_id == user.id,
            DailyEventIndex.day == day
        ).all()
        if not rows:
            _build_daily_event_index(user)
            rows = db.query(DailyEventIndex).filter(
                DailyEventIndex.user_id == user.id,
                DailyEventIndex.day == day
            ).all()
        return format_events(rows, user.timezone)
    finally:
        db.close()

def _done_events(user: User, nums: list[int]) -> str:
    """
    Kept for future (event-done tracking), but currently unused by commands.
    """
    day = today_in_tz(user.timezone)
    db = SessionLocal()
    try:
        mapping = db.query(DailyEventIndex).filter(
            DailyEventIndex.user_id == user.id,
            DailyEventIndex.day == day
        ).all()
        by_num = {m.event_number: m for m in mapping}

        missing = [n for n in nums if n not in by_num]
        if missing:
            return f"These event numbers don’t exist today: {missing}\nRun EVENTS TODAY first."

        lines = ["✅ Marked done:"]
        for n in nums:
            ev = by_num[n]
            existing = db.query(EventDone).filter(
                EventDone.user_id == user.id,
                EventDone.day == day,
                EventDone.google_event_id == ev.google_event_id
            ).one_or_none()
            if not existing:
                db.add(EventDone(user_id=user.id, day=day, google_event_id=ev.google_event_id))
            lines.append(f"- {n}) {ev.title}")

        db.commit()
        return "\n".join(lines)
    finally:
        db.close()
