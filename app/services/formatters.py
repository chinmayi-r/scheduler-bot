from __future__ import annotations

from datetime import date, datetime
import pytz

from ..db import Person, DailyEventIndex
from ..services.timeutil import today_in_tz
from ..services.todoist import TodoistTask


# ── People ────────────────────────────────────────────────────────────────────

def _person_contact_status(p: Person, tz_name: str) -> dict | None:
    """
    Returns contact status if base_days is set, else None.
    overdue_by > 0  → overdue by that many days
    overdue_by == 0 → due today
    overdue_by < 0  → days remaining until due
    """
    if p.base_days is None:
        return None
    today = today_in_tz(tz_name)
    ref = p.last_contact or p.start_day
    if ref is None:
        return None
    days_since = (today - ref).days
    overdue_by = days_since - int(p.base_days)
    return {"days_since": days_since, "overdue_by": overdue_by}


def _person_is_due(p: Person, tz_name: str) -> bool:
    status = _person_contact_status(p, tz_name)
    return status is not None and status["overdue_by"] >= 0


def format_people(people: list[Person], tz_name: str, due_only: bool = False) -> str:
    if not people:
        return "No people saved."

    people_sorted = sorted(people, key=lambda p: (-p.priority, p.name.lower()))

    if due_only:
        people_sorted = [p for p in people_sorted if _person_is_due(p, tz_name)]
        if not people_sorted:
            return "No one due for contact today. ✅"

    lines = []
    for i, p in enumerate(people_sorted, start=1):
        status = _person_contact_status(p, tz_name)
        if status is not None:
            ob = status["overdue_by"]
            if ob > 0:
                contact_txt = f" ⚠️ {ob}d overdue"
            elif ob == 0:
                contact_txt = " — due today"
            else:
                contact_txt = f" — in {-ob}d"
        else:
            contact_txt = ""
        note_txt = f" — {p.note}" if p.note else ""
        lines.append(f"{i}) (P{p.priority}) {p.name}{contact_txt}{note_txt}")

    return "\n".join(lines)


# ── Events ────────────────────────────────────────────────────────────────────

def format_events(events: list[DailyEventIndex], tz_name: str = "UTC") -> str:
    if not events:
        return "No timed events found for today."

    tz = pytz.timezone(tz_name)
    lines = []
    for e in sorted(events, key=lambda e: e.event_number):
        start = e.start_dt if e.start_dt.tzinfo else pytz.utc.localize(e.start_dt)
        end   = e.end_dt   if e.end_dt.tzinfo   else pytz.utc.localize(e.end_dt)
        s = start.astimezone(tz).strftime("%H:%M")
        en = end.astimezone(tz).strftime("%H:%M")
        lines.append(f"{e.event_number}) {s}-{en} {e.title}")

    return "\n".join(lines)


# ── Todos ─────────────────────────────────────────────────────────────────────

def format_todoist_tasks_numbered(tasks: list[TodoistTask], tz_name: str = "UTC") -> str:
    if not tasks:
        return "No active tasks."

    tz = pytz.timezone(tz_name)
    now = datetime.now(tz)
    lines = []

    for i, t in enumerate(tasks, start=1):
        due_txt = ""
        overdue = False

        if t.due:
            if t.due.get("datetime"):
                try:
                    dt = datetime.fromisoformat(t.due["datetime"].replace("Z", "+00:00"))
                    dt_local = dt.astimezone(tz)
                    due_txt = f" — due {dt_local.strftime('%a %H:%M')}"
                    overdue = dt_local < now
                except Exception:
                    due_txt = f" — due {t.due['datetime']}"
            elif t.due.get("date"):
                try:
                    d = date.fromisoformat(t.due["date"])
                    due_txt = f" — due {d.strftime('%a %b %d')}"
                    overdue = d < now.date()
                except Exception:
                    due_txt = f" — due {t.due['date']}"
            elif t.due.get("string"):
                due_txt = f" — due {t.due['string']}"

        if overdue:
            due_txt += " ⚠️ overdue"

        lines.append(f"{i}) {t.content}{due_txt}")

    return "\n".join(lines)
