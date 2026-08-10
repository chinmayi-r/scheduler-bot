from __future__ import annotations

from telegram import (
    InlineKeyboardButton, InlineKeyboardMarkup,
    KeyboardButton, ReplyKeyboardMarkup,
)

from .services.todoist import TodoistTask
from .db import Person


# ── Persistent keyboard ───────────────────────────────────────────────────────
# Sits above the text input permanently. This is the closest thing Telegram has
# to "always on": the app stays visible without having to remember it exists.

BTN_TODAY = "📋 Today"
BTN_TASKS = "✅ Tasks"
BTN_CAPTURE = "➕ Capture"
BTN_BREAKDOWN = "🔨 Break down"
BTN_MORE = "⚙️ More"

PERSISTENT_BUTTONS = {BTN_TODAY, BTN_TASKS, BTN_CAPTURE, BTN_BREAKDOWN, BTN_MORE}


def persistent_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton(BTN_TODAY), KeyboardButton(BTN_TASKS)],
            [KeyboardButton(BTN_BREAKDOWN), KeyboardButton(BTN_MORE)],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Type anything → it becomes a task",
    )


# ── Onboarding / settings pickers ─────────────────────────────────────────────

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


def tz_confirm(prefix: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ That's right", callback_data=f"{prefix}:ok"),
        InlineKeyboardButton("🔁 Wrong, change it", callback_data=f"{prefix}:redo"),
    ]])


def time_picker(prefix: str, options: list[str]) -> InlineKeyboardMarkup:
    row = [InlineKeyboardButton(t, callback_data=f"{prefix}:{t}") for t in options]
    return InlineKeyboardMarkup([row, [InlineKeyboardButton("Custom time", callback_data=f"{prefix}:custom")]])


def cancel_only() -> InlineKeyboardMarkup:
    """Every multi-step flow carries this, so an abandoned flow is always one
    tap from being escaped instead of silently eating the next message."""
    return InlineKeyboardMarkup([[InlineKeyboardButton("✖️ Cancel", callback_data="cancelflow")]])


# ── Planning ──────────────────────────────────────────────────────────────────

MARK_ON = "☑️"
MARK_OFF = "⬜"


def morning_plan_keyboard(tasks: list[TodoistTask], selected_ids: set[str]) -> InlineKeyboardMarkup:
    rows = []
    for t in tasks[:10]:  # keep the picker itself from becoming overwhelming
        mark = MARK_ON if t.id in selected_ids else MARK_OFF
        rows.append([InlineKeyboardButton(f"{mark} {t.content[:40]}", callback_data=f"plantgl:{t.id}")])
    rows.append([
        InlineKeyboardButton("✅ Confirm plan", callback_data="planconfirm"),
        InlineKeyboardButton("😴 Skip today", callback_data="planskip"),
    ])
    return InlineKeyboardMarkup(rows)


def toggle_plan_markup(markup: InlineKeyboardMarkup, task_id: str) -> InlineKeyboardMarkup:
    """Flips one checkbox using the live message as the source of truth, so a
    redeploy mid-planning can't wipe the selection out from under you."""
    rows = []
    for row in markup.inline_keyboard:
        new_row = []
        for btn in row:
            if btn.callback_data == f"plantgl:{task_id}":
                label = btn.text
                if label.startswith(MARK_ON):
                    label = MARK_OFF + label[len(MARK_ON):]
                elif label.startswith(MARK_OFF):
                    label = MARK_ON + label[len(MARK_OFF):]
                new_row.append(InlineKeyboardButton(label, callback_data=btn.callback_data))
            else:
                new_row.append(btn)
        rows.append(new_row)
    return InlineKeyboardMarkup(rows)


def selected_from_markup(markup: InlineKeyboardMarkup) -> list[str]:
    out = []
    for row in markup.inline_keyboard:
        for btn in row:
            data = btn.callback_data or ""
            if data.startswith("plantgl:") and btn.text.startswith(MARK_ON):
                out.append(data.split(":", 1)[1])
    return out


def count_selected(markup: InlineKeyboardMarkup) -> int:
    return len(selected_from_markup(markup))


# ── Tasks ─────────────────────────────────────────────────────────────────────

def task_actions_keyboard(task_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Done", callback_data=f"done:{task_id}"),
        InlineKeyboardButton("🔨 Break down", callback_data=f"breakdown:{task_id}"),
    ]])


def tasks_list_keyboard(tasks: list[TodoistTask], *, with_breakdown: bool = True) -> InlineKeyboardMarkup:
    rows = []
    for t in tasks[:12]:
        row = [InlineKeyboardButton(f"✅ {t.content[:32]}", callback_data=f"done:{t.id}")]
        if with_breakdown:
            row.append(InlineKeyboardButton("🔨", callback_data=f"breakdown:{t.id}"))
        rows.append(row)
    return InlineKeyboardMarkup(rows)


def breakdown_pick_keyboard(tasks: list[TodoistTask]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(t.content[:40], callback_data=f"breakdown:{t.id}")] for t in tasks[:10]]
    return InlineKeyboardMarkup(rows)


def breakdown_result_keyboard(task_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("➕ Add steps as tasks", callback_data=f"bdadd:{task_id}"),
        InlineKeyboardButton("✅ Done", callback_data=f"done:{task_id}"),
    ]])


# ── Meals / people ────────────────────────────────────────────────────────────

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
            InlineKeyboardButton("💤", callback_data=f"person:{p.id}:snooze"),
            InlineKeyboardButton("📝", callback_data=f"personnote:{p.id}"),
        ])
    return InlineKeyboardMarkup(rows)


# ── Help / more ───────────────────────────────────────────────────────────────

def help_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📥 How capture works", callback_data="help:capture")],
        [InlineKeyboardButton("🔁 Daily rhythm", callback_data="help:rhythm")],
        [InlineKeyboardButton("🔥 How the streak works", callback_data="help:streak")],
        [InlineKeyboardButton("👥 People & 🍽️ meals", callback_data="help:extras")],
        [InlineKeyboardButton("⚙️ Settings", callback_data="help:settings")],
    ])


def more_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👥 People", callback_data="more:people"),
         InlineKeyboardButton("🍽️ Meals", callback_data="more:meals")],
        [InlineKeyboardButton("🔥 Streak", callback_data="more:streak")],
        [InlineKeyboardButton("⚙️ Settings", callback_data="more:settings"),
         InlineKeyboardButton("❓ Help", callback_data="more:help")],
    ])
