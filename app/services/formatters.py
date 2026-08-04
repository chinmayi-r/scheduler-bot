from __future__ import annotations

from datetime import date, datetime
import pytz

from .gcal import CalEvent
from .todoist import TodoistTask


def format_events(events: list[CalEvent], tz_name: str = "UTC") -> str:
    if not events:
        return "Nothing on the calendar today."

    tz = pytz.timezone(tz_name)
    lines = []
    for i, e in enumerate(sorted(events, key=lambda e: e.start_utc), start=1):
        s = e.start_utc.astimezone(tz).strftime("%H:%M")
        en = e.end_utc.astimezone(tz).strftime("%H:%M")
        lines.append(f"{i}) {s}-{en} {e.title}")
    return "\n".join(lines)


def format_tasks_numbered(tasks: list[TodoistTask], tz_name: str = "UTC") -> str:
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
