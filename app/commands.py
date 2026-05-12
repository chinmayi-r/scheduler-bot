from __future__ import annotations

import re
from datetime import datetime

from .db import SessionLocal, User, Person, Note, DailyEventIndex
from .config import DEFAULT_TIMEZONE
from .services.timeutil import today_in_tz
from .services.gcal import fetch_events_for_day_multi_ics
from .services.todoist import (
    add_task as todoist_add_task,
    list_active_tasks as todoist_list_tasks,
    close_task as todoist_close_task,
    TodoistError,
    default_project_id,
)
from .services.formatters import format_people, format_events, format_todoist_tasks_numbered


# ── Regex ─────────────────────────────────────────────────────────────────────

PERSON_ADD_RE = re.compile(
    r"^\s*PERSON\s+ADD\s*->\s*(?P<name>[^|]+?)"
    r"(?:\s*\|\s*p\s*=\s*(?P<prio>\d+))?"
    r"(?:\s*\|\s*note\s*=\s*(?P<note>[^|]+?))?"
    r"(?:\s*\|\s*days\s*=\s*(?P<days>-?\d+))?\s*$",
    re.IGNORECASE,
)
PERSON_LIST_RE  = re.compile(r"^\s*PERSON\s+LIST\s*$", re.IGNORECASE)
PERSON_DEL_RE   = re.compile(r"^\s*PERSON\s+DEL\s*->\s*(.+?)\s*$", re.IGNORECASE)
PERSON_QUICK_RE = re.compile(r"^\s*PERSON\s*->\s*([^,]+?)\s*,\s*(-?\d+)\s*$", re.IGNORECASE)

EVENTS_TODAY_RE   = re.compile(r"^\s*EVENTS\s+TODAY\s*$", re.IGNORECASE)
EVENTS_REFRESH_RE = re.compile(r"^\s*EVENTS\s+REFRESH\s*$", re.IGNORECASE)

TODO_ADD_RE  = re.compile(r"^\s*TODO\s+ADD\s*->\s*(.+?)(?:\s*\|\s*due\s*=\s*(.+))?\s*$", re.IGNORECASE)
TODO_LIST_RE = re.compile(r"^\s*TODO\s+LIST\s*$", re.IGNORECASE)
DONE_TODO_RE = re.compile(r"^\s*DONE\s+TODO\s*->\s*([0-9,\s]+)\s*$", re.IGNORECASE)

TZ_SET_RE = re.compile(r"^\s*TZ\s*->\s*(.+?)\s*$", re.IGNORECASE)
ACMD_RE   = re.compile(r"^\s*ACMD\s*=\s*(.+?)\s*$", re.IGNORECASE)


# ── TODO LIST cache (chat_id → tasks, for DONE TODO numbering) ────────────────

TODO_LIST_CACHE: dict[str, list] = {}


# ── Help text ─────────────────────────────────────────────────────────────────

def help_text() -> str:
    return (
        "Quick shortcuts (send HELP FULL for all commands):\n\n"
        "ACMD=todo buy milk\n"
        "ACMD=todo laundry due thursday\n"
        "ACMD=list              (show todos)\n"
        "ACMD=done todo 1 2 3\n"
        "ACMD=events            (today's calendar)\n"
        "ACMD=refresh           (rebuild calendar)\n"
        "ACMD=people            (show people list)\n"
        "ACMD=person alice 3    (track alice, reach out every 3 days)\n"
        "ACMD=contact alice     (log that you reached out)\n"
        "ACMD=del alice         (remove person)\n"
        "ACMD=tz America/New_York\n\n"
        "/status  /streak\n"
        "Anything else → saved as a note."
    )


def help_text_full() -> str:
    return (
        "All commands:\n\n"
        "PERSON-> name, days\n"
        "PERSON ADD -> name | p=8 | note=one line | days=3\n"
        "PERSON LIST\n"
        "PERSON DEL -> name\n\n"
        "EVENTS TODAY\n"
        "EVENTS REFRESH\n\n"
        "TODO ADD -> task\n"
        "TODO ADD -> task | due=Thursday\n"
        "TODO LIST\n"
        "DONE TODO -> 1,2,3\n\n"
        "TZ-> Area/City\n\n"
        "/status  /streak\n"
        "Anything else → saved as a note."
    )


# ── DB helper ─────────────────────────────────────────────────────────────────

def _get_or_create_user(db, telegram_chat_id: str) -> User:
    user = db.query(User).filter(User.telegram_chat_id == telegram_chat_id).one_or_none()
    if user is None:
        user = User(telegram_chat_id=telegram_chat_id, timezone=DEFAULT_TIMEZONE)
        db.add(user)
        db.commit()
        db.refresh(user)
    return user


# ── Command handlers ──────────────────────────────────────────────────────────

def _cmd_tz_set(db, user: User, tz: str) -> str:
    user.timezone = tz
    db.commit()
    return f"✅ Timezone set to {tz}."


def _cmd_person_upsert(db, user: User, name: str, base_days: int | None,
                       priority: int = 5, note: str = "") -> str:
    today = today_in_tz(user.timezone)
    existing = db.query(Person).filter(
        Person.user_id == user.id, Person.name.ilike(name)
    ).one_or_none()
    if existing:
        existing.name = name
        existing.priority = priority
        existing.note = note
        if base_days is not None:
            existing.base_days = base_days
            existing.start_day = today
        existing.updated_at = datetime.utcnow()
        db.commit()
        freq = f" (every {base_days}d)" if base_days else ""
        return f"✅ Updated: {name} (P{priority}){freq}"
    else:
        db.add(Person(
            user_id=user.id,
            name=name,
            priority=priority,
            note=note,
            base_days=base_days,
            start_day=today if base_days is not None else None,
        ))
        db.commit()
        freq = f" (every {base_days}d)" if base_days else ""
        return f"✅ Added: {name} (P{priority}){freq}"


def _cmd_person_list(db, user: User) -> str:
    people = db.query(Person).filter(Person.user_id == user.id).all()
    return format_people(people, user.timezone)


def _cmd_person_del(db, user: User, name: str) -> str:
    obj = db.query(Person).filter(
        Person.user_id == user.id, Person.name.ilike(name)
    ).one_or_none()
    if not obj:
        return f"No person named '{name}'."
    db.delete(obj)
    db.commit()
    return f"🗑️ Removed: {obj.name}"


def _cmd_person_contact(db, user: User, name: str) -> str:
    obj = db.query(Person).filter(
        Person.user_id == user.id, Person.name.ilike(name)
    ).one_or_none()
    if not obj:
        return f"No person named '{name}'."
    obj.last_contact = today_in_tz(user.timezone)
    obj.updated_at = datetime.utcnow()
    db.commit()
    return f"✅ Logged contact with {obj.name}."


def _cmd_events_today(db, user: User) -> str:
    return _events_today(user)


def _cmd_events_refresh(db, user: User) -> str:
    _build_daily_event_index(user)
    user.needs_reschedule = True
    db.commit()
    return "✅ Refreshed.\n\n" + _events_today(user)


def _cmd_todo_add(task: str, due: str | None) -> str:
    if not task:
        return "Task text can't be empty."
    try:
        todoist_add_task(task, project_id=default_project_id(), due_string=due)
        suffix = f" (due {due})" if due else ""
        return f"✅ Added: {task}{suffix}"
    except TodoistError as e:
        return f"Todoist error: {e}"


def _cmd_todo_list(chat_id: str) -> str:
    try:
        tasks = todoist_list_tasks(project_id=default_project_id())
        TODO_LIST_CACHE[chat_id] = tasks
        return format_todoist_tasks_numbered(tasks)
    except TodoistError as e:
        return f"Todoist error: {e}"


def _cmd_todo_done(chat_id: str, nums_raw: str) -> str:
    tasks = TODO_LIST_CACHE.get(chat_id, [])
    if not tasks:
        return "Run ACMD=list first so I know which task numbers you mean."

    nums: list[int] = []
    for part in re.split(r"[,\s]+", nums_raw.strip()):
        if not part:
            continue
        try:
            nums.append(int(part))
        except ValueError:
            return "Invalid numbers. Example: DONE TODO -> 1,2,3"

    missing = [n for n in nums if n < 1 or n > len(tasks)]
    if missing:
        return f"Task numbers {missing} don't exist. Run ACMD=list again."

    try:
        lines = []
        for n in nums:
            t = tasks[n - 1]
            todoist_close_task(t.id)
            lines.append(f"- {n}) {t.content}")
        return "✅ Completed:\n" + "\n".join(lines)
    except TodoistError as e:
        return f"Todoist error: {e}"


# ── ACMD dispatcher ───────────────────────────────────────────────────────────

def _handle_acmd(db, user: User, chat_id: str, s: str) -> str:
    low = s.lower().strip()

    if low in {"events", "today", "calendar"}:
        return _cmd_events_today(db, user)

    if low in {"refresh"}:
        return _cmd_events_refresh(db, user)

    if low in {"people", "person list"}:
        return _cmd_person_list(db, user)

    if low in {"list", "todos", "todo list"}:
        return _cmd_todo_list(chat_id)

    m = re.match(r"^todo\s+(.+?)(?:\s+due\s+(.+))?$", s, re.IGNORECASE)
    if m:
        return _cmd_todo_add(m.group(1).strip(), (m.group(2) or "").strip() or None)

    m = re.match(r"^done\s+todo\s+([0-9,\s]+)$", s, re.IGNORECASE)
    if m:
        return _cmd_todo_done(chat_id, m.group(1))

    m = re.match(r"^person\s+(.+?)\s+(-?\d+)$", s, re.IGNORECASE)
    if m:
        return _cmd_person_upsert(db, user, m.group(1).strip(), int(m.group(2)))

    m = re.match(r"^contact\s+(.+)$", s, re.IGNORECASE)
    if m:
        return _cmd_person_contact(db, user, m.group(1).strip())

    m = re.match(r"^del\s+(.+)$", s, re.IGNORECASE)
    if m:
        return _cmd_person_del(db, user, m.group(1).strip())

    m = re.match(r"^tz\s+(.+)$", s, re.IGNORECASE)
    if m:
        return _cmd_tz_set(db, user, m.group(1).strip())

    return (
        "ACMD not recognized. Try:\n"
        "ACMD=todo <task> / ACMD=list / ACMD=done todo 1 2\n"
        "ACMD=events / refresh / people\n"
        "ACMD=person <name> <days> / ACMD=contact <name> / ACMD=del <name>\n"
        "ACMD=tz Area/City"
    )


# ── Main entry point ──────────────────────────────────────────────────────────

def handle_text_command(chat_id: str, text: str) -> str:
    db = SessionLocal()
    try:
        user = _get_or_create_user(db, chat_id)
        raw = (text or "").strip()

        if raw.upper() in {"HELP", "COMMANDS"}:
            return help_text()

        if raw.upper() in {"HELP FULL", "COMMANDS FULL"}:
            return help_text_full()

        m = ACMD_RE.match(raw)
        if m:
            return _handle_acmd(db, user, chat_id, m.group(1).strip())

        m = TZ_SET_RE.match(raw)
        if m:
            return _cmd_tz_set(db, user, m.group(1).strip())

        m = PERSON_QUICK_RE.match(raw)
        if m:
            return _cmd_person_upsert(db, user, m.group(1).strip(), int(m.group(2)))

        m = PERSON_ADD_RE.match(raw)
        if m:
            name = m.group("name").strip()
            prio = int(m.group("prio") or 5)
            note = (m.group("note") or "").strip()
            days = m.group("days")
            if not (1 <= prio <= 10):
                return "Priority must be 1–10."
            return _cmd_person_upsert(db, user, name, int(days) if days else None, prio, note)

        if PERSON_LIST_RE.match(raw):
            return _cmd_person_list(db, user)

        m = PERSON_DEL_RE.match(raw)
        if m:
            return _cmd_person_del(db, user, m.group(1).strip())

        if EVENTS_TODAY_RE.match(raw):
            return _cmd_events_today(db, user)

        if EVENTS_REFRESH_RE.match(raw):
            return _cmd_events_refresh(db, user)

        m = TODO_ADD_RE.match(raw)
        if m:
            return _cmd_todo_add(m.group(1).strip(), (m.group(2) or "").strip() or None)

        if TODO_LIST_RE.match(raw):
            return _cmd_todo_list(chat_id)

        m = DONE_TODO_RE.match(raw)
        if m:
            return _cmd_todo_done(chat_id, m.group(1))

        if raw:
            db.add(Note(user_id=user.id, day=today_in_tz(user.timezone), text=raw))
            db.commit()
            return "📝 Saved."

        return help_text()
    finally:
        db.close()


# ── Event index helpers (also used by scheduler) ──────────────────────────────

def _build_daily_event_index(user: User) -> None:
    day = today_in_tz(user.timezone)
    events = fetch_events_for_day_multi_ics(user.timezone, day)

    db = SessionLocal()
    try:
        db.query(DailyEventIndex).filter(
            DailyEventIndex.user_id == user.id, DailyEventIndex.day == day
        ).delete()
        for i, ev in enumerate(sorted(events, key=lambda e: e.start_utc), start=1):
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
            DailyEventIndex.user_id == user.id, DailyEventIndex.day == day
        ).all()
        if not rows:
            _build_daily_event_index(user)
            rows = db.query(DailyEventIndex).filter(
                DailyEventIndex.user_id == user.id, DailyEventIndex.day == day
            ).all()
        return format_events(rows, user.timezone)
    finally:
        db.close()
