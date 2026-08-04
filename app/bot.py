from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, BotCommand
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters,
)

from .config import (
    TELEGRAM_BOT_TOKEN, BOT_INSTANCE_LOCK, GCAL_ICS_URLS_JSON_DEFAULT,
    POCKET_WEBHOOK_SECRET,
)
from .db import init_db, SessionLocal, User, DailyLog, Person, InboxSuggestion
from . import keyboards
from .keyboards import (
    BTN_TODAY, BTN_TASKS, BTN_CAPTURE, BTN_BREAKDOWN, BTN_MORE, PERSISTENT_BUTTONS,
)
from .services.timeutil import today_in_tz, now_in_tz, parse_hhmm, valid_timezone
from .services.todoist import list_active_tasks, close_task, TodoistError, TodoistTask
from .services.capture import quick_capture
from .services.gcal import fetch_events_for_day_multi_ics
from .services.formatters import format_events, format_tasks_numbered
from .services import people as people_svc
from .services import meals as meals_svc
from .services import voice as voice_svc
from .services import llm as llm_svc
from .services.streaks import get_or_create_log, format_streak_line
from .scheduler import schedule_user_jobs, schedule_all_active_users, mark_nudge_responded


# A flow left half-finished must never swallow the next thing you type. State
# expires, and expired input falls through to capture rather than vanishing.
AWAITING_TTL_SECONDS = 15 * 60


def _set_awaiting(context, kind: str, **extra) -> None:
    context.user_data["awaiting"] = {"kind": kind, "ts": time.time(), **extra}


def _get_awaiting(context) -> dict | None:
    state = context.user_data.get("awaiting")
    if not isinstance(state, dict):
        return None
    if time.time() - state.get("ts", 0) > AWAITING_TTL_SECONDS:
        context.user_data.pop("awaiting", None)
        return None
    return state


def _clear_awaiting(context) -> None:
    context.user_data.pop("awaiting", None)


# ── Views (shared by commands, persistent buttons, and the More menu) ─────────

def _today_view(db, user: User) -> tuple[str, InlineKeyboardMarkup | None]:
    day = today_in_tz(user.timezone)
    urls = json.loads(user.gcal_ics_urls_json or "{}")
    events = fetch_events_for_day_multi_ics(urls, user.timezone, day)

    log = get_or_create_log(db, user, day)
    planned_ids = json.loads(log.planned_task_ids_json or "[]")
    completed_ids = set(json.loads(log.completed_task_ids_json or "[]"))

    try:
        all_tasks = {t.id: t for t in list_active_tasks()}
    except TodoistError:
        all_tasks = {}

    if planned_ids:
        lines = []
        for tid in planned_ids:
            content = all_tasks[tid].content if tid in all_tasks else "(done or removed)"
            lines.append(f"{'✅' if tid in completed_ids else '⬜'} {content}")
        plan_txt = "\n".join(lines)
    else:
        plan_txt = "Nothing picked yet. Text me anything and it lands here."

    parts = [f"📅 {format_events(events, user.timezone)}", f"\n📋 Today's plan:\n{plan_txt}"]

    if user.people_enabled:
        due = people_svc.people_due_today(db, user, user.timezone)
        if due:
            listing = "\n".join(f"• {people_svc.format_person_line(p, day)}" for p in due)
            parts.append(f"\n👥 Reach out to:\n{listing}")

    if user.meals_enabled:
        statuses = meals_svc.today_meal_status(db, user, day)
        parts.append(f"\n🍽️ {meals_svc.format_meal_summary(user, statuses).replace(chr(10), '  ')}")

    parts.append(f"\n{format_streak_line(db, user, day)}")

    remaining = [all_tasks[t] for t in planned_ids if t in all_tasks and t not in completed_ids]
    markup = keyboards.tasks_list_keyboard(remaining) if remaining else None
    return "\n".join(parts), markup


def _tasks_view(db, user: User) -> tuple[str, InlineKeyboardMarkup | None]:
    try:
        tasks = list_active_tasks()
    except TodoistError as e:
        return f"Couldn't reach Todoist: {e}", None
    if not tasks:
        return "No active tasks. Text me anything to add one.", None
    extra = "\n\n(showing first 12 — tap ✅ to finish, 🔨 to break one down)" if len(tasks) > 12 else ""
    return format_tasks_numbered(tasks, user.timezone) + extra, keyboards.tasks_list_keyboard(tasks)


def _people_view(db, user: User) -> tuple[str, InlineKeyboardMarkup]:
    if not user.people_enabled:
        return "People tracking is off — turn it on in ⚙️ Settings.", keyboards.more_keyboard()
    day = today_in_tz(user.timezone)
    ppl = people_svc.list_people(db, user)
    if not ppl:
        text = "No one saved yet.\n\nAdd someone and I'll remind you before you accidentally ghost them."
    else:
        text = "\n".join(f"• {people_svc.format_person_line(p, day)}" for p in ppl)
        text += "\n\n✅ = logged contact · 💤 = snooze 3d · 📝 = add a note"
    rows = list(keyboards.people_due_keyboard(ppl).inline_keyboard) if ppl else []
    rows.append([InlineKeyboardButton("➕ Add person", callback_data="peopleadd:start")])
    return text, InlineKeyboardMarkup(rows)


def _meals_view(db, user: User) -> tuple[str, InlineKeyboardMarkup | None]:
    if not user.meals_enabled:
        return "Meal check-ins are off — turn them on in ⚙️ Settings.", keyboards.more_keyboard()
    day = today_in_tz(user.timezone)
    statuses = meals_svc.today_meal_status(db, user, day)
    rows = []
    for meal in meals_svc.load_meal_times(user):
        if statuses.get(meal) is None:
            rows.append([
                InlineKeyboardButton(f"🍽️ {meal.capitalize()}", callback_data=f"meal:{meal}:ate"),
                InlineKeyboardButton("⏭️ Skip", callback_data=f"meal:{meal}:skip"),
            ])
    text = meals_svc.format_meal_summary(user, statuses)
    if not rows:
        text += "\n\nAll logged for today."
    return text, InlineKeyboardMarkup(rows) if rows else None


def _streak_view(db, user: User) -> tuple[str, None]:
    day = today_in_tz(user.timezone)
    return (
        format_streak_line(db, user, day)
        + "\n\nA day counts if you did *anything*: picked a plan, finished one task, "
          "or tapped the wind-down. Miss one day and nothing resets.",
        None,
    )


def _inbox_view(db, user: User) -> tuple[str, InlineKeyboardMarkup | None]:
    rows = (
        db.query(InboxSuggestion)
        .filter(InboxSuggestion.user_id == user.id, InboxSuggestion.status == "pending")
        .order_by(InboxSuggestion.created_at.desc())
        .all()
    )
    if not rows:
        if not POCKET_WEBHOOK_SECRET:
            return ("Inbox is empty.\n\nConnect your Pocket recorder (set POCKET_WEBHOOK_SECRET) "
                    "and action items from your conversations will land here for one-tap adding."), None
        return "Inbox is empty — nothing waiting from Pocket.", None
    listing = "\n".join(f"• {r.text}" for r in rows)
    return f"📥 {len(rows)} waiting from Pocket:\n{listing}", keyboards.suggestions_keyboard(rows)


def _settings_view(db, user: User) -> tuple[str, InlineKeyboardMarkup]:
    local = now_in_tz(user.timezone).strftime("%H:%M")
    text = (
        f"⚙️ Settings\n\n"
        f"🕐 Timezone: {user.timezone} — it's {local} for you right now\n"
        f"🌅 Morning plan: {user.morning_time}\n"
        f"🌆 Wind-down: {user.evening_time}\n"
        f"🍽️ Meals: {'on' if user.meals_enabled else 'off'}\n"
        f"👥 People: {'on' if user.people_enabled else 'off'}\n"
        f"⏰ Re-pings when ignored: {'on' if user.escalation_enabled else 'off'}"
    )
    rows = [
        [InlineKeyboardButton("🕐 Timezone", callback_data="settings:tz"),
         InlineKeyboardButton("🌅 Morning", callback_data="settings:morning")],
        [InlineKeyboardButton("🌆 Evening", callback_data="settings:evening"),
         InlineKeyboardButton("📅 Calendar", callback_data="settings:addcal")],
        [InlineKeyboardButton(f"🍽️ Meals: {'on' if user.meals_enabled else 'off'}",
                              callback_data="settings:mealstoggle"),
         InlineKeyboardButton(f"👥 People: {'on' if user.people_enabled else 'off'}",
                              callback_data="settings:peopletoggle")],
        [InlineKeyboardButton(f"⏰ Re-pings: {'on' if user.escalation_enabled else 'off'}",
                              callback_data="settings:esctoggle")],
    ]
    return text, InlineKeyboardMarkup(rows)


HELP_SECTIONS = {
    "capture": (
        "📥 Capture\n\n"
        "Type anything. That's it — it becomes a task in Todoist immediately. "
        "No syntax, no commands, no picking a project.\n\n"
        "You can also send a voice note and it gets transcribed into a task, "
        "which is usually faster than typing when you're mid-thought.\n\n"
        "Tasks live in Todoist, so you can open the real app any time to "
        "reorganise, add due dates, or use a widget."
    ),
    "rhythm": (
        "🔁 The daily rhythm\n\n"
        "🌅 Morning — I show your tasks and you tap up to 5 for today. Small on purpose.\n"
        "🕐 Midday — I resurface whatever's still open, so it can't quietly disappear.\n"
        "🌆 Evening — quick wind-down; whatever didn't happen is fine.\n\n"
        "Ignore a prompt and I'll nudge again a few times rather than giving up. "
        "Change the times or turn the re-pings off in ⚙️ Settings."
    ),
    "streak": (
        "🔥 The streak\n\n"
        "A day counts if you did *anything at all* — picked a plan, finished one "
        "task, or tapped wind-down.\n\n"
        "Missing one day does nothing. Only two misses in a row reset the count, "
        "and that resets a number, not you. Flexible tracking like this lasts "
        "far longer than 'perfect streak or bust'."
    ),
    "extras": (
        "👥 People\n"
        "Saved contacts hold real notes — what they do, what you last talked about — "
        "so a reminder gives you something to open with instead of a blank countdown. "
        "Tap 📝 on anyone to add a note.\n\n"
        "🍽️ Meals\n"
        "Gentle check-ins at your set times. Tap 'Ate it', 'Skip', or just send a "
        "photo and it attaches automatically."
    ),
    "settings": (
        "⚙️ Settings\n\n"
        "Change your timezone, the three ping times, and toggle meals / people / "
        "re-pings. Settings show your current local time so you can confirm the "
        "timezone is actually right.\n\n"
        "Everything applies immediately — no restart, no redeploy."
    ),
}


def _help_text() -> str:
    return (
        "Type anything → it becomes a task. That's the whole app.\n\n"
        "The buttons below the keyboard are always there:\n"
        f"{BTN_TODAY} · {BTN_TASKS} · {BTN_BREAKDOWN} · {BTN_MORE}\n\n"
        "🔨 Break down turns a task that feels too big into small steps, "
        "with a first step under 2 minutes.\n\n"
        "Tap below for anything else, or use /cancel if you ever get stuck mid-flow."
    )


# ── User + onboarding helpers ─────────────────────────────────────────────────

def _get_or_create_user(db, chat_id: str) -> tuple[User, bool]:
    user = db.query(User).filter(User.telegram_chat_id == chat_id).one_or_none()
    if user is not None:
        return user, False
    user = User(telegram_chat_id=chat_id, state="onb_tz")
    if GCAL_ICS_URLS_JSON_DEFAULT:
        try:
            json.loads(GCAL_ICS_URLS_JSON_DEFAULT)
            user.gcal_ics_urls_json = GCAL_ICS_URLS_JSON_DEFAULT
        except Exception:
            pass
    db.add(user)
    db.commit()
    db.refresh(user)
    return user, True


async def _send_onboarding_step(chat_id: int, user: User, context: ContextTypes.DEFAULT_TYPE) -> None:
    step = user.state
    bot = context.bot

    if step == "onb_tz":
        await bot.send_message(
            chat_id,
            "Let's set this up — six taps, then you never have to think about it again.\n\n"
            "First: what timezone are you in?",
            reply_markup=keyboards.tz_picker("onbtz"),
        )
    elif step == "onb_tz_custom":
        await bot.send_message(chat_id, "Type your timezone, e.g. Europe/Berlin or Asia/Tokyo.",
                               reply_markup=keyboards.cancel_only())
    elif step == "onb_tz_confirm":
        local = now_in_tz(user.timezone).strftime("%H:%M")
        await bot.send_message(
            chat_id,
            f"It's *{local}* for you right now, going by {user.timezone}.\n\n"
            "If that's wrong the nudges land at the wrong time, so worth checking.",
            parse_mode="Markdown",
            reply_markup=keyboards.tz_confirm("onbtzc"),
        )
    elif step == "onb_morning":
        await bot.send_message(
            chat_id,
            "When should I ask you to pick today's tasks?\n\n"
            "This is the one that stops the day disappearing on you.",
            reply_markup=keyboards.time_picker("onbmorning", ["07:00", "08:00", "09:00"]),
        )
    elif step == "onb_morning_custom":
        await bot.send_message(chat_id, "Type a 24h time as HH:MM, e.g. 07:30.",
                               reply_markup=keyboards.cancel_only())
    elif step == "onb_evening":
        await bot.send_message(
            chat_id,
            "And an evening wind-down, to close the day out?",
            reply_markup=keyboards.time_picker("onbevening", ["20:00", "21:00", "22:00"]),
        )
    elif step == "onb_evening_custom":
        await bot.send_message(chat_id, "Type a 24h time as HH:MM, e.g. 21:30.",
                               reply_markup=keyboards.cancel_only())
    elif step == "onb_meals":
        await bot.send_message(
            chat_id, "Want meal check-ins? (Tap 'Ate it' / 'Skip', or just send a photo.)",
            reply_markup=keyboards.yes_no("onbmeals"),
        )
    elif step == "onb_people":
        await bot.send_message(
            chat_id,
            "Want reminders to stay in touch with people? They hold real notes, "
            "so you get context — not just a countdown.",
            reply_markup=keyboards.yes_no("onbpeople"),
        )
    elif step == "onb_calendar":
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("Skip", callback_data="onbcal:skip")]])
        await bot.send_message(
            chat_id, "Last one — paste a calendar ICS link to see today's events, or skip.",
            reply_markup=kb,
        )


async def _finish_onboarding(db, user: User, context: ContextTypes.DEFAULT_TYPE) -> None:
    user.state = "active"
    db.commit()
    schedule_user_jobs(context.application, user)
    local = now_in_tz(user.timezone).strftime("%H:%M")
    await context.bot.send_message(
        int(user.telegram_chat_id),
        f"✅ Done. It's {local} for you; I'll ping at {user.morning_time} and {user.evening_time}.\n\n"
        f"The buttons below are always there. Type anything to capture it.",
        reply_markup=keyboards.persistent_keyboard(),
    )


# ── Commands ──────────────────────────────────────────────────────────────────

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _clear_awaiting(context)
    chat_id = str(update.effective_chat.id)
    db = SessionLocal()
    try:
        user, _ = _get_or_create_user(db, chat_id)
        if user.state == "active":
            await update.message.reply_text(
                "You're all set up.\n\n" + _help_text(),
                reply_markup=keyboards.persistent_keyboard(),
            )
            return
        await _send_onboarding_step(update.effective_chat.id, user, context)
    finally:
        db.close()


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _clear_awaiting(context)
    await update.message.reply_text(_help_text(), reply_markup=keyboards.persistent_keyboard())
    await update.message.reply_text("What do you want to know more about?",
                                    reply_markup=keyboards.help_keyboard())


async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    had = _get_awaiting(context) is not None
    _clear_awaiting(context)
    await update.message.reply_text(
        "Cancelled — back to normal. Anything you type now becomes a task."
        if had else "Nothing to cancel. Type anything to capture it.",
        reply_markup=keyboards.persistent_keyboard(),
    )


def _simple_command(view_fn):
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        _clear_awaiting(context)
        chat_id = str(update.effective_chat.id)
        db = SessionLocal()
        try:
            user = db.query(User).filter(User.telegram_chat_id == chat_id).one_or_none()
            if not user or user.state != "active":
                await update.message.reply_text("Send /start first — takes about 20 seconds.")
                return
            text, markup = view_fn(db, user)
            await update.message.reply_text(text, reply_markup=markup)
        finally:
            db.close()
    return handler


today_cmd = _simple_command(_today_view)
tasks_cmd = _simple_command(_tasks_view)
people_cmd = _simple_command(_people_view)
meals_cmd = _simple_command(_meals_view)
streak_cmd = _simple_command(_streak_view)
settings_cmd = _simple_command(_settings_view)
inbox_cmd = _simple_command(_inbox_view)


async def breakdown_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _clear_awaiting(context)
    chat_id = str(update.effective_chat.id)
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.telegram_chat_id == chat_id).one_or_none()
        if not user or user.state != "active":
            await update.message.reply_text("Send /start first.")
            return
        if not llm_svc.llm_enabled():
            await update.message.reply_text(
                "Break-down needs an LLM key. Set LLM_API_KEY — OpenRouter's free tier works fine."
            )
            return
        try:
            tasks = list_active_tasks()
        except TodoistError as e:
            await update.message.reply_text(f"Couldn't reach Todoist: {e}")
            return
        if not tasks:
            await update.message.reply_text("No tasks to break down yet.")
            return
        await update.message.reply_text(
            "Which one feels too big to start?",
            reply_markup=keyboards.breakdown_pick_keyboard(tasks),
        )
    finally:
        db.close()


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    print("ERROR:", repr(context.error))


# ── Breakdown helpers ─────────────────────────────────────────────────────────

_STEP_RE = re.compile(r"^\s*\d+\.\s+(.+?)\s*$", re.MULTILINE)


def _format_steps(task_content: str, steps: list[str]) -> str:
    listing = "\n".join(f"{i}. {s}" for i, s in enumerate(steps, start=1))
    return (
        f"🔨 {task_content}\n\n{listing}\n\n"
        f"Start with step 1 — it's meant to take under two minutes."
    )


def _steps_from_message(text: str) -> list[str]:
    """Recovers the steps from the message itself so the Add button keeps
    working across restarts without holding server-side state."""
    return [m.strip() for m in _STEP_RE.findall(text or "")]


async def _do_breakdown(query, db, user: User, task_id: str) -> None:
    try:
        tasks = {t.id: t for t in list_active_tasks()}
    except TodoistError as e:
        await query.message.reply_text(f"Couldn't reach Todoist: {e}")
        return

    task = tasks.get(task_id)
    if not task:
        await query.message.reply_text("That task isn't active any more.")
        return

    await query.message.reply_text("Thinking…")
    try:
        steps = llm_svc.break_down_task(task.content)
    except llm_svc.LLMError as e:
        await query.message.reply_text(
            f"Couldn't break that down: {e}\n\n"
            f"Try again in a moment — free-tier models get busy."
        )
        return

    await query.message.reply_text(
        _format_steps(task.content, steps),
        reply_markup=keyboards.breakdown_result_keyboard(task_id),
    )


# ── Callback handler ──────────────────────────────────────────────────────────

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    data = query.data or ""
    chat_id = str(update.effective_chat.id)

    db = SessionLocal()
    try:
        user, _ = _get_or_create_user(db, chat_id)

        # ── Universal escape ──
        if data == "cancelflow":
            _clear_awaiting(context)
            await query.answer("Cancelled")
            await query.message.reply_text(
                "Cancelled. Anything you type now becomes a task.",
                reply_markup=keyboards.persistent_keyboard(),
            )
            return

        # ── Onboarding ──
        if data.startswith("onbtz:"):
            val = data.split(":", 1)[1]
            if val == "custom":
                user.state = "onb_tz_custom"
            else:
                user.timezone = val
                user.state = "onb_tz_confirm"
            db.commit()
            await query.answer()
            await _send_onboarding_step(update.effective_chat.id, user, context)
            return

        if data.startswith("onbtzc:"):
            if data.endswith(":redo"):
                user.state = "onb_tz"
            else:
                user.state = "onb_morning"
            db.commit()
            await query.answer()
            await _send_onboarding_step(update.effective_chat.id, user, context)
            return

        if data.startswith("onbmorning:"):
            val = data.split(":", 1)[1]
            if val == "custom":
                user.state = "onb_morning_custom"
            else:
                user.morning_time = val
                user.state = "onb_evening"
            db.commit()
            await query.answer()
            await _send_onboarding_step(update.effective_chat.id, user, context)
            return

        if data.startswith("onbevening:"):
            val = data.split(":", 1)[1]
            if val == "custom":
                user.state = "onb_evening_custom"
            else:
                user.evening_time = val
                user.state = "onb_meals"
            db.commit()
            await query.answer()
            await _send_onboarding_step(update.effective_chat.id, user, context)
            return

        if data.startswith("onbmeals:"):
            user.meals_enabled = data.endswith(":yes")
            user.state = "onb_people"
            db.commit()
            await query.answer()
            await _send_onboarding_step(update.effective_chat.id, user, context)
            return

        if data.startswith("onbpeople:"):
            user.people_enabled = data.endswith(":yes")
            user.state = "onb_calendar"
            db.commit()
            await query.answer()
            await _send_onboarding_step(update.effective_chat.id, user, context)
            return

        if data == "onbcal:skip":
            await query.answer()
            await _finish_onboarding(db, user, context)
            return

        # ── More menu / help ──
        if data.startswith("more:"):
            which = data.split(":", 1)[1]
            views = {
                "people": _people_view, "meals": _meals_view, "streak": _streak_view,
                "settings": _settings_view, "inbox": _inbox_view,
            }
            await query.answer()
            if which == "help":
                await query.message.reply_text(_help_text(), reply_markup=keyboards.help_keyboard())
                return
            text, markup = views[which](db, user)
            await query.message.reply_text(text, reply_markup=markup)
            return

        if data.startswith("help:"):
            section = data.split(":", 1)[1]
            await query.answer()
            await query.message.reply_text(
                HELP_SECTIONS.get(section, _help_text()),
                reply_markup=keyboards.help_keyboard(),
            )
            return

        # ── Settings ──
        if data == "settings:tz":
            await query.answer()
            await query.message.reply_text("Pick your timezone:", reply_markup=keyboards.tz_picker("settz"))
            return

        if data.startswith("settz:"):
            val = data.split(":", 1)[1]
            if val == "custom":
                _set_awaiting(context, "settings_tz_custom")
                await query.answer()
                await query.message.reply_text("Type your timezone, e.g. Europe/Berlin.",
                                               reply_markup=keyboards.cancel_only())
                return
            user.timezone = val
            db.commit()
            schedule_user_jobs(context.application, user)
            local = now_in_tz(user.timezone).strftime("%H:%M")
            await query.answer("Updated")
            await query.message.reply_text(
                f"✅ {val} — it's {local} for you now. Pings rescheduled to match."
            )
            return

        if data in ("settings:morning", "settings:evening"):
            which = "morning" if data.endswith("morning") else "evening"
            opts = ["07:00", "08:00", "09:00"] if which == "morning" else ["20:00", "21:00", "22:00"]
            await query.answer()
            await query.message.reply_text(
                f"New {which} time:", reply_markup=keyboards.time_picker(f"set{which}", opts)
            )
            return

        if data.startswith("setmorning:") or data.startswith("setevening:"):
            which = "morning" if data.startswith("setmorning:") else "evening"
            val = data.split(":", 1)[1]
            if val == "custom":
                _set_awaiting(context, f"settings_{which}_custom")
                await query.answer()
                await query.message.reply_text("Type a 24h time as HH:MM.",
                                               reply_markup=keyboards.cancel_only())
                return
            setattr(user, f"{which}_time", val)
            db.commit()
            schedule_user_jobs(context.application, user)
            await query.answer("Updated")
            await query.message.reply_text(f"✅ {which.capitalize()} ping set to {val}.")
            return

        if data == "settings:mealstoggle":
            user.meals_enabled = not user.meals_enabled
            db.commit()
            schedule_user_jobs(context.application, user)
            await query.answer()
            text, markup = _settings_view(db, user)
            await query.edit_message_text(text, reply_markup=markup)
            return

        if data == "settings:peopletoggle":
            user.people_enabled = not user.people_enabled
            db.commit()
            await query.answer()
            text, markup = _settings_view(db, user)
            await query.edit_message_text(text, reply_markup=markup)
            return

        if data == "settings:esctoggle":
            user.escalation_enabled = not user.escalation_enabled
            db.commit()
            await query.answer()
            text, markup = _settings_view(db, user)
            await query.edit_message_text(text, reply_markup=markup)
            return

        if data == "settings:addcal":
            _set_awaiting(context, "settings_cal_custom")
            await query.answer()
            await query.message.reply_text("Paste the ICS link.", reply_markup=keyboards.cancel_only())
            return

        # ── Planning (state lives in the message, so restarts can't break it) ──
        if data.startswith("plantgl:"):
            task_id = data.split(":", 1)[1]
            markup = query.message.reply_markup
            if markup is None:
                await query.answer("This plan message is too old — send /today.")
                return
            already = keyboards.selected_from_markup(markup)
            if task_id not in already and len(already) >= 5:
                await query.answer("Five is plenty for one day.", show_alert=True)
                return
            await query.answer()
            await query.edit_message_reply_markup(
                reply_markup=keyboards.toggle_plan_markup(markup, task_id)
            )
            return

        if data == "planconfirm":
            markup = query.message.reply_markup
            selected = keyboards.selected_from_markup(markup) if markup else []
            day = today_in_tz(user.timezone)
            log = get_or_create_log(db, user, day)
            log.planned_task_ids_json = json.dumps(selected)
            log.morning_responded_at = datetime.utcnow()
            db.commit()
            mark_nudge_responded(db, user, day, "morning")
            await query.answer("Plan set")

            if not selected:
                await query.edit_message_text("Nothing picked — that's logged as showing up too. 🔥")
                return
            try:
                lookup = {t.id: t.content for t in list_active_tasks()}
            except TodoistError:
                lookup = {}
            listing = "\n".join(f"• {lookup.get(t, '(task)')}" for t in selected)
            await query.edit_message_text(f"📋 Today:\n{listing}")
            return

        if data == "planskip":
            day = today_in_tz(user.timezone)
            log = get_or_create_log(db, user, day)
            log.morning_responded_at = datetime.utcnow()
            db.commit()
            mark_nudge_responded(db, user, day, "morning")
            await query.answer("Skipped")
            await query.edit_message_text(
                "😴 Skipped — and that still counts as showing up. Evening check-in later."
            )
            return

        # ── Tasks ──
        if data.startswith("done:"):
            task_id = data.split(":", 1)[1]
            try:
                close_task(task_id)
            except TodoistError as e:
                await query.answer(f"Todoist error: {e}", show_alert=True)
                return
            day = today_in_tz(user.timezone)
            log = get_or_create_log(db, user, day)
            completed = set(json.loads(log.completed_task_ids_json or "[]"))
            completed.add(task_id)
            log.completed_task_ids_json = json.dumps(list(completed))
            db.commit()
            await query.answer("✅ Done")
            markup = query.message.reply_markup
            if markup:
                rows = [
                    [b for b in row if not (b.callback_data or "").endswith(f":{task_id}")
                     and (b.callback_data or "") != f"done:{task_id}"]
                    for row in markup.inline_keyboard
                ]
                rows = [r for r in rows if r]
                await query.edit_message_reply_markup(
                    reply_markup=InlineKeyboardMarkup(rows) if rows else None
                )
            return

        if data.startswith("breakdown:"):
            await query.answer()
            await _do_breakdown(query, db, user, data.split(":", 1)[1])
            return

        if data.startswith("bdadd:"):
            steps = _steps_from_message(query.message.text or "")
            if not steps:
                await query.answer("Couldn't read the steps.", show_alert=True)
                return
            added = 0
            for step in steps:
                try:
                    quick_capture(db, user, step)
                    added += 1
                except TodoistError:
                    break
            await query.answer(f"Added {added}")
            await query.message.reply_text(f"➕ Added {added} step(s) as their own tasks.")
            return

        # ── Meals ──
        if data.startswith("meal:"):
            _, meal, action = data.split(":", 2)
            day = today_in_tz(user.timezone)
            status = "logged" if action == "ate" else "skipped"
            meals_svc.log_meal(db, user, day, meal, status)
            mark_nudge_responded(db, user, day, "meal", meal)
            await query.answer("Logged")
            await query.edit_message_text(
                f"{'✅' if status == 'logged' else '⏭️'} {meal.capitalize()} logged."
            )
            return

        # ── People ──
        if data.startswith("personnote:"):
            pid = int(data.split(":", 1)[1])
            person = people_svc.get_by_id(db, user, pid)
            if not person:
                await query.answer("Not found")
                return
            _set_awaiting(context, "person_note", person_id=pid)
            await query.answer()
            await query.message.reply_text(
                f"What should I remember about {person.name}?",
                reply_markup=keyboards.cancel_only(),
            )
            return

        if data.startswith("person:"):
            _, pid_str, action = data.split(":", 2)
            person = people_svc.get_by_id(db, user, int(pid_str))
            if not person:
                await query.answer("Not found")
                return
            if action == "contacted":
                people_svc.mark_contacted_obj(db, person, user.timezone)
                await query.answer("Logged")
                await query.message.reply_text(f"✅ Logged contact with {person.name}.")
            else:
                people_svc.snooze_obj(db, person, user.timezone, days=3)
                await query.answer("Snoozed")
                await query.message.reply_text(f"💤 {person.name} snoozed for 3 days.")
            return

        if data == "peopleadd:start":
            _set_awaiting(context, "people_add_name")
            await query.answer()
            await query.message.reply_text("What's their name?", reply_markup=keyboards.cancel_only())
            return

        # ── Pocket inbox suggestions ──
        if data.startswith("sugg:"):
            _, sid, action = data.split(":", 2)
            row = db.query(InboxSuggestion).filter(
                InboxSuggestion.id == int(sid), InboxSuggestion.user_id == user.id
            ).one_or_none()
            if not row or row.status != "pending":
                await query.answer("Already handled")
                return
            if action == "add":
                try:
                    quick_capture(db, user, row.text)
                except TodoistError as e:
                    await query.answer(f"Todoist error: {e}", show_alert=True)
                    return
                row.status = "added"
                await query.answer("Added")
            else:
                row.status = "dismissed"
                await query.answer("Dismissed")
            db.commit()
            text, markup = _inbox_view(db, user)
            await query.edit_message_text(text, reply_markup=markup)
            return

        if data.startswith("suggall:"):
            action = data.split(":", 1)[1]
            rows = db.query(InboxSuggestion).filter(
                InboxSuggestion.user_id == user.id, InboxSuggestion.status == "pending"
            ).all()
            count = 0
            for row in rows:
                if action == "add":
                    try:
                        quick_capture(db, user, row.text)
                    except TodoistError:
                        break
                    row.status = "added"
                else:
                    row.status = "dismissed"
                count += 1
            db.commit()
            await query.answer(f"{'Added' if action == 'add' else 'Dismissed'} {count}")
            await query.edit_message_text(
                f"{'➕ Added' if action == 'add' else '🗑️ Dismissed'} {count} item(s)."
            )
            return

        if data == "eveningdone":
            day = today_in_tz(user.timezone)
            log = get_or_create_log(db, user, day)
            log.evening_responded_at = datetime.utcnow()
            db.commit()
            mark_nudge_responded(db, user, day, "evening")
            await query.answer("Noted")
            await query.edit_message_text("✅ Day closed. 🔥")
            return

        await query.answer()
    finally:
        db.close()


# ── Text ──────────────────────────────────────────────────────────────────────

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (update.message.text or "").strip()
    if not text:
        return
    chat_id = str(update.effective_chat.id)

    db = SessionLocal()
    try:
        user, _ = _get_or_create_user(db, chat_id)

        if user.state != "active":
            await _handle_onboarding_text(update, context, db, user, text)
            return

        # Persistent-keyboard taps arrive as ordinary text; they must be handled
        # before capture or tapping "Today" would create a task called "Today".
        if text in PERSISTENT_BUTTONS:
            _clear_awaiting(context)
            if text == BTN_TODAY:
                body, markup = _today_view(db, user)
            elif text == BTN_TASKS:
                body, markup = _tasks_view(db, user)
            elif text == BTN_MORE:
                body, markup = "What do you need?", keyboards.more_keyboard()
            elif text == BTN_CAPTURE:
                body, markup = "Go ahead — type it and it's captured.", None
            else:  # BTN_BREAKDOWN
                await breakdown_cmd(update, context)
                return
            await update.message.reply_text(body, reply_markup=markup)
            return

        awaiting = _get_awaiting(context)
        if awaiting:
            await _handle_awaiting_text(update, context, db, user, awaiting, text)
            return

        # A text reply also answers an open evening check-in, without costing the capture.
        day = today_in_tz(user.timezone)
        log = get_or_create_log(db, user, day)
        if log.evening_prompted_at is not None and log.evening_responded_at is None:
            log.evening_responded_at = datetime.utcnow()
            db.commit()
            mark_nudge_responded(db, user, day, "evening")

        try:
            task = quick_capture(db, user, text)
        except TodoistError as e:
            await update.message.reply_text(
                f"Couldn't save that to Todoist: {e}\n\nYour text: {text}"
            )
            return
        await update.message.reply_text(
            f"📥 {task.content}",
            reply_markup=keyboards.task_actions_keyboard(task.id),
        )
    finally:
        db.close()


async def _handle_onboarding_text(update, context, db, user: User, text: str) -> None:
    step = user.state
    chat_id = update.effective_chat.id

    if step == "onb_tz_custom":
        if not valid_timezone(text):
            await update.message.reply_text("Not a valid timezone. Try e.g. Europe/Berlin.")
            return
        user.timezone = text
        user.state = "onb_tz_confirm"
        db.commit()
        await _send_onboarding_step(chat_id, user, context)
        return

    if step in ("onb_morning_custom", "onb_evening_custom"):
        try:
            t = parse_hhmm(text)
        except Exception:
            await update.message.reply_text("Use 24h HH:MM, e.g. 07:30.")
            return
        hhmm = f"{t.hour:02d}:{t.minute:02d}"
        if step == "onb_morning_custom":
            user.morning_time, user.state = hhmm, "onb_evening"
        else:
            user.evening_time, user.state = hhmm, "onb_meals"
        db.commit()
        await _send_onboarding_step(chat_id, user, context)
        return

    if step == "onb_calendar":
        urls = json.loads(user.gcal_ics_urls_json or "{}")
        urls["primary"] = text
        user.gcal_ics_urls_json = json.dumps(urls)
        db.commit()
        await _finish_onboarding(db, user, context)
        return

    await update.message.reply_text("Tap one of the buttons above to keep going.")


async def _handle_awaiting_text(update, context, db, user: User, awaiting: dict, text: str) -> None:
    kind = awaiting.get("kind")

    if kind == "settings_tz_custom":
        if not valid_timezone(text):
            await update.message.reply_text("Not a valid timezone. Try e.g. Europe/Berlin, or /cancel.")
            return
        user.timezone = text
        db.commit()
        schedule_user_jobs(context.application, user)
        _clear_awaiting(context)
        local = now_in_tz(user.timezone).strftime("%H:%M")
        await update.message.reply_text(f"✅ {text} — it's {local} for you now.")
        return

    if kind in ("settings_morning_custom", "settings_evening_custom"):
        try:
            t = parse_hhmm(text)
        except Exception:
            await update.message.reply_text("Use 24h HH:MM, e.g. 07:30, or /cancel.")
            return
        hhmm = f"{t.hour:02d}:{t.minute:02d}"
        if kind == "settings_morning_custom":
            user.morning_time = hhmm
        else:
            user.evening_time = hhmm
        db.commit()
        schedule_user_jobs(context.application, user)
        _clear_awaiting(context)
        await update.message.reply_text(f"✅ Set to {hhmm}.")
        return

    if kind == "settings_cal_custom":
        urls = json.loads(user.gcal_ics_urls_json or "{}")
        urls["primary"] = text
        user.gcal_ics_urls_json = json.dumps(urls)
        db.commit()
        _clear_awaiting(context)
        await update.message.reply_text("✅ Calendar saved.")
        return

    if kind == "person_note":
        person = people_svc.get_by_id(db, user, awaiting.get("person_id"))
        _clear_awaiting(context)
        if not person:
            await update.message.reply_text("That person's gone now.")
            return
        people_svc.update_context(db, user, person.name, text, tz_name=user.timezone)
        await update.message.reply_text(f"📝 Noted about {person.name}.")
        return

    if kind == "people_add_name":
        _set_awaiting(context, "people_add_cadence", name=text)
        await update.message.reply_text(
            "Remind you every how many days? (a number, or 'never')",
            reply_markup=keyboards.cancel_only(),
        )
        return

    if kind == "people_add_cadence":
        cadence = None
        if text.strip().lower() not in ("never", "no", "n/a", "none"):
            try:
                cadence = int(text.strip())
            except ValueError:
                await update.message.reply_text("A number of days, or 'never'. Or /cancel.")
                return
        _set_awaiting(context, "people_add_context", name=awaiting.get("name"), cadence=cadence)
        await update.message.reply_text(
            "Anything to remember about them? (or 'skip')",
            reply_markup=keyboards.cancel_only(),
        )
        return

    if kind == "people_add_context":
        name = awaiting.get("name")
        cadence = awaiting.get("cadence")
        _clear_awaiting(context)
        note = "" if text.strip().lower() == "skip" else text.strip()
        people_svc.add_person(db, user, name, cadence, note)
        await update.message.reply_text(f"✅ Saved {name}.")
        return

    # Unknown state: never swallow the message -- fall through to capture.
    _clear_awaiting(context)
    try:
        task = quick_capture(db, user, text)
        await update.message.reply_text(f"📥 {task.content}")
    except TodoistError as e:
        await update.message.reply_text(f"Couldn't save that: {e}")


# ── Photo + voice ─────────────────────────────────────────────────────────────

async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = str(update.effective_chat.id)
    caption = (update.message.caption or "").strip()

    db = SessionLocal()
    try:
        user, _ = _get_or_create_user(db, chat_id)
        if user.state != "active":
            await update.message.reply_text("Send /start first.")
            return

        day = today_in_tz(user.timezone)
        photo = update.message.photo[-1]

        if user.meals_enabled:
            from .db import PendingNudge
            pending = (
                db.query(PendingNudge)
                .filter(PendingNudge.user_id == user.id, PendingNudge.day == day,
                        PendingNudge.kind == "meal", PendingNudge.responded_at.is_(None))
                .order_by(PendingNudge.last_sent_at.desc())
                .first()
            )
            if pending:
                meals_svc.log_meal(db, user, day, pending.ref, "logged",
                                   note=caption or None, photo_file_id=photo.file_id)
                mark_nudge_responded(db, user, day, "meal", pending.ref)
                await update.message.reply_text(f"📷 Logged for {pending.ref}.")
                return

            # No meal pending: attach to the next unlogged meal rather than
            # rejecting the photo outright.
            statuses = meals_svc.today_meal_status(db, user, day)
            unlogged = [m for m in meals_svc.load_meal_times(user) if m not in statuses]
            if unlogged:
                meal = unlogged[0]
                meals_svc.log_meal(db, user, day, meal, "logged",
                                   note=caption or None, photo_file_id=photo.file_id)
                await update.message.reply_text(f"📷 Logged as {meal}.")
                return

        if caption:
            try:
                task = quick_capture(db, user, caption)
                await update.message.reply_text(f"📥 Captured the caption: {task.content}")
                return
            except TodoistError:
                pass
        await update.message.reply_text("📷 Got it — nothing was waiting on a photo.")
    finally:
        db.close()


async def on_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not voice_svc.voice_enabled():
        await update.message.reply_text(
            "Voice capture needs OPENAI_API_KEY set. Type it instead for now."
        )
        return

    tg_file = await update.message.voice.get_file()
    audio = bytes(await tg_file.download_as_bytearray())
    try:
        text = voice_svc.transcribe_ogg(audio)
    except Exception as e:
        await update.message.reply_text(f"Couldn't transcribe that: {e}")
        return

    if not text:
        await update.message.reply_text("Didn't catch anything in that.")
        return

    chat_id = str(update.effective_chat.id)
    db = SessionLocal()
    try:
        user, _ = _get_or_create_user(db, chat_id)
        if user.state != "active":
            await update.message.reply_text("Finish /start setup first.")
            return
        task = quick_capture(db, user, text)
        await update.message.reply_text(
            f"🎙️ {task.content}", reply_markup=keyboards.task_actions_keyboard(task.id)
        )
    except TodoistError as e:
        await update.message.reply_text(f"Heard \"{text}\" but couldn't save it: {e}")
    finally:
        db.close()


# ── Entry point ───────────────────────────────────────────────────────────────

BOT_COMMANDS = [
    BotCommand("today", "Everything for today"),
    BotCommand("tasks", "Active tasks, tap to finish"),
    BotCommand("breakdown", "Split a task into small steps"),
    BotCommand("inbox", "Action items waiting from Pocket"),
    BotCommand("people", "People to stay in touch with"),
    BotCommand("meals", "Meal check-ins"),
    BotCommand("streak", "How you're doing"),
    BotCommand("settings", "Times, timezone, toggles"),
    BotCommand("cancel", "Get unstuck"),
    BotCommand("help", "How this works"),
]


async def _post_init(application: Application) -> None:
    schedule_all_active_users(application)
    # Populates Telegram's Menu button, so commands are discoverable instead of
    # something you have to remember.
    await application.bot.set_my_commands(BOT_COMMANDS)

    if POCKET_WEBHOOK_SECRET:
        from .webhook import start_webhook_server
        application.bot_data["webhook_runner"] = await start_webhook_server(application)


async def _post_shutdown(application: Application) -> None:
    runner = application.bot_data.get("webhook_runner")
    if runner is not None:
        await runner.cleanup()


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _acquire_instance_lock(lock_path: str) -> None:
    """Guards against two pollers in one container. Critically, a lock left
    behind by a crashed process must not block the restart forever — that turns
    a one-off crash into a permanently dead bot."""
    for _attempt in range(2):
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            return
        except FileExistsError:
            try:
                with open(lock_path) as fh:
                    prev_pid = int((fh.read() or "0").strip())
            except (ValueError, OSError):
                prev_pid = 0

            if prev_pid and prev_pid != os.getpid() and _pid_alive(prev_pid):
                print(f"Bot already running (pid {prev_pid}). Exiting.")
                sys.exit(0)

            print(f"Removing stale lock from dead pid {prev_pid or 'unknown'}.")
            try:
                os.unlink(lock_path)
            except FileNotFoundError:
                pass

    print("Could not acquire instance lock. Exiting.")
    sys.exit(0)


def main() -> None:
    if BOT_INSTANCE_LOCK == "1":
        _acquire_instance_lock("/tmp/tg_bot.lock")

    init_db()

    app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("cancel", cancel_cmd))
    app.add_handler(CommandHandler("today", today_cmd))
    app.add_handler(CommandHandler("tasks", tasks_cmd))
    app.add_handler(CommandHandler("breakdown", breakdown_cmd))
    app.add_handler(CommandHandler("inbox", inbox_cmd))
    app.add_handler(CommandHandler("people", people_cmd))
    app.add_handler(CommandHandler("meals", meals_cmd))
    app.add_handler(CommandHandler("streak", streak_cmd))
    app.add_handler(CommandHandler("settings", settings_cmd))

    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(filters.VOICE, on_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    app.add_error_handler(on_error)

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
