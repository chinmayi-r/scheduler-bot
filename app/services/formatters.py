from __future__ import annotations

from datetime import date, datetime
import pytz

from ..db import Person, DailyEventIndex
from ..services.timeutil import today_in_tz
from ..services.todoist import TodoistTask


def person_days_now(p: Person, tz_name: str) -> int | None:
    if p.base_days is None or p.start_day is None:
        return None
    today = today_in_tz(tz_name)
    return int(p.base_days) + (today - p.start_day).days


def format_people(people: list[Person], tz_name: str) -> str:
    if not people:
        return "No people saved."

    people_sorted = sorted(people, key=lambda p: (-p.priority, p.name.lower()))
    lines: list[str] = []
    for i, p in enumerate(people_sorted, start=1):
        dn = person_days_now(p, tz_name)
        days_txt = f" — {dn} days" if dn is not None else ""
        lines.append(f"{i}) (P{p.priority}) {p.name}{days_txt} — {p.note}")
    return "\n".join(lines)


def format_events(events: list[DailyEventIndex], tz_name: str = "UTC") -> str:
    if not events:
        return "No timed events found for today."

    tz = pytz.timezone(tz_name)
    events_sorted = sorted(events, key=lambda e: e.event_number)

    lines: list[str] = []
    for e in events_sorted:
        start = e.start_dt
        end = e.end_dt

        if start.tzinfo is None:
            start = pytz.utc.localize(start)
        if end.tzinfo is None:
            end = pytz.utc.localize(end)

        start_local = start.astimezone(tz)
        end_local = end.astimezone(tz)

        lines.append(
            f"{e.event_number}) {start_local.strftime('%H:%M')}-{end_local.strftime('%H:%M')} {e.title}"
        )

    return "\n".join(lines)


def format_todoist_tasks_numbered(tasks: list[TodoistTask]) -> str:
    if not tasks:
        return "No active tasks."

    lines = []
    for i, t in enumerate(tasks, start=1):

        due_txt = ""
        if t.due:
            # Todoist due object can contain:
            # - due["string"] (human readable)
            # - due["date"] (ISO date)
            # - due["datetime"] (ISO datetime)

            if t.due.get("string"):
                due_txt = f" — due {t.due['string']}"
            elif t.due.get("datetime"):
                try:
                    dt = datetime.fromisoformat(t.due["datetime"].replace("Z", "+00:00"))
                    due_txt = f" — due {dt.strftime('%a %H:%M')}"
                except Exception:
                    due_txt = f" — due {t.due['datetime']}"
            elif t.due.get("date"):
                due_txt = f" — due {t.due['date']}"
                
        if t.due and t.due.get("is_overdue"):
            due_txt += " ⚠️ overdue"

        lines.append(f"{i}) {t.content}{due_txt}")

    return "\n".join(lines)

