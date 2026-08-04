from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from .services.todoist import TodoistTask
from .db import Person


def yes_no(prefix: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("Yes", callback_data=f"{prefix}:yes"),
        InlineKeyboardButton("No", callback_data=f"{prefix}:no"),
    ]])


def tz_picker(prefix: str) -> InlineKeyboardMarkup:
    common = [
        ("America/New_York", "US Eastern"),
        ("America/Chicago", "US Central"),
        ("America/Denver", "US Mountain"),
        ("America/Los_Angeles", "US Pacific"),
        ("Europe/London", "UK"),
        ("Asia/Kolkata", "India"),
    ]
    rows = [[InlineKeyboardButton(label, callback_data=f"{prefix}:{tz}")] for tz, label in common]
    rows.append([InlineKeyboardButton("Type it myself", callback_data=f"{prefix}:custom")])
    return InlineKeyboardMarkup(rows)


def time_picker(prefix: str, options: list[str]) -> InlineKeyboardMarkup:
    row = [InlineKeyboardButton(t, callback_data=f"{prefix}:{t}") for t in options]
    return InlineKeyboardMarkup([row, [InlineKeyboardButton("Custom time", callback_data=f"{prefix}:custom")]])


def morning_plan_keyboard(tasks: list[TodoistTask], selected_ids: set[str]) -> InlineKeyboardMarkup:
    rows = []
    for t in tasks[:10]:  # keep the picker itself from becoming overwhelming
        mark = "☑️" if t.id in selected_ids else "⬜"
        label = f"{mark} {t.content[:40]}"
        rows.append([InlineKeyboardButton(label, callback_data=f"plantgl:{t.id}")])
    rows.append([
        InlineKeyboardButton("✅ Confirm plan", callback_data="planconfirm"),
        InlineKeyboardButton("😴 Skip today", callback_data="planskip"),
    ])
    return InlineKeyboardMarkup(rows)


def task_done_keyboard(task_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("✅ Done", callback_data=f"done:{task_id}")]])


def tasks_list_keyboard(tasks: list[TodoistTask]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(f"✅ {t.content[:40]}", callback_data=f"done:{t.id}")] for t in tasks[:15]]
    return InlineKeyboardMarkup(rows)


def meal_keyboard(meal: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🍽️ Ate it", callback_data=f"meal:{meal}:ate"),
        InlineKeyboardButton("⏭️ Skip", callback_data=f"meal:{meal}:skip"),
    ]])


def person_keyboard(person: Person) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Contacted", callback_data=f"person:{person.id}:contacted"),
        InlineKeyboardButton("💤 Snooze 3d", callback_data=f"person:{person.id}:snooze"),
    ]])


def people_due_keyboard(people: list[Person]) -> InlineKeyboardMarkup:
    rows = []
    for p in people[:10]:
        rows.append([
            InlineKeyboardButton(f"✅ {p.name}", callback_data=f"person:{p.id}:contacted"),
            InlineKeyboardButton("💤 Snooze", callback_data=f"person:{p.id}:snooze"),
        ])
    return InlineKeyboardMarkup(rows)
