from __future__ import annotations

import json
import os
import sys
from datetime import datetime

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters,
)

from .config import TELEGRAM_BOT_TOKEN, BOT_INSTANCE_LOCK, GCAL_ICS_URLS_JSON_DEFAULT
from .db import init_db, SessionLocal, User, DailyLog, Person
from . import keyboards
from .services.timeutil import today_in_tz, parse_hhmm, valid_timezone
from .services.todoist import list_active_tasks, close_task, TodoistError, TodoistTask
from .services.capture import quick_capture
from .services.gcal import fetch_events_for_day_multi_ics
from .services.formatters import format_events, format_tasks_numbered
from .services import people as people_svc
from .services import meals as meals_svc
from .services import voice as voice_svc
from .services.streaks import get_or_create_log, format_streak_line
from .scheduler import schedule_user_jobs, schedule_all_active_users, mark_nudge_responded


HELP_TEXT = (
    "Nothing here needs syntax. Just text me anything and it becomes a task.\n\n"
    "/today — everything visible at once (calendar, today's plan, people, meals)\n"
    "/tasks — your active tasks, tap to mark done\n"
    "/people — people you're tracking, tap to log contact\n"
    "/meals — today's meal check-ins\n"
    "/streak — how you're doing (forgiving, not punishing)\n"
    "/settings — change timezone, ping times, toggles\n"
    "/help — this message\n\n"
    "Send a photo or voice note any time — voice gets transcribed and captured just like text."
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
            "Let's set this up properly. First — what timezone are you in? "
            "(This is the thing that was broken before — nudges will actually land at the right "
            "local time now.)",
            reply_markup=keyboards.tz_picker("onbtz"),
        )
    elif step == "onb_tz_custom":
        await bot.send_message(chat_id, "Type your IANA timezone, e.g. Europe/Berlin or Asia/Tokyo.")
    elif step == "onb_morning":
        await bot.send_message(
            chat_id,
            "What time do you want a daily 'plan today' ping? Pick a small handful of tasks each morning — "
            "that's the whole trick against 'tasks don't exist if I don't look.'",
            reply_markup=keyboards.time_picker("onbmorning", ["07:00", "08:00", "09:00"]),
        )
    elif step == "onb_morning_custom":
        await bot.send_message(chat_id, "Type a 24h time as HH:MM, e.g. 07:30.")
    elif step == "onb_evening":
        await bot.send_message(
            chat_id,
            "And a wind-down check-in time, to close the day out and forgive whatever didn't happen?",
            reply_markup=keyboards.time_picker("onbevening", ["20:00", "21:00", "22:00"]),
        )
    elif step == "onb_evening_custom":
        await bot.send_message(chat_id, "Type a 24h time as HH:MM, e.g. 21:30.")
    elif step == "onb_meals":
        await bot.send_message(
            chat_id,
            "Want meal check-ins too? You mentioned regular meals are tough — a nudge + easy photo log can help.",
            reply_markup=keyboards.yes_no("onbmeals"),
        )
    elif step == "onb_people":
        await bot.send_message(
            chat_id,
            "Want people-contact reminders? These hold actual notes on each person (not just a countdown), "
            "so a reminder tells you what you last talked about.",
            reply_markup=keyboards.yes_no("onbpeople"),
        )
    elif step == "onb_calendar":
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("Skip", callback_data="onbcal:skip")]])
        await bot.send_message(
            chat_id,
            "Last thing — paste a calendar ICS link if you want today's events shown (optional), or tap Skip.",
            reply_markup=kb,
        )


async def _finish_onboarding(db, user: User, context: ContextTypes.DEFAULT_TYPE) -> None:
    user.state = "active"
    db.commit()
    schedule_user_jobs(context.application, user)
    await context.bot.send_message(
        int(user.telegram_chat_id),
        f"✅ All set. Morning ping {user.morning_time}, wind-down {user.evening_time}, "
        f"timezone {user.timezone}.\n\n{HELP_TEXT}",
    )


# ── Command handlers ──────────────────────────────────────────────────────────

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = str(update.effective_chat.id)
    db = SessionLocal()
    try:
        user, _created = _get_or_create_user(db, chat_id)
        if user.state == "active":
            await update.message.reply_text(f"You're already set up.\n\n{HELP_TEXT}")
            return
        await _send_onboarding_step(update.effective_chat.id, user, context)
    finally:
        db.close()


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP_TEXT)


async def today_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = str(update.effective_chat.id)
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.telegram_chat_id == chat_id).one_or_none()
        if not user or user.state != "active":
            await update.message.reply_text("Run /start first.")
            return

        day = today_in_tz(user.timezone)
        urls = json.loads(user.gcal_ics_urls_json or "{}")
        events = fetch_events_for_day_multi_ics(urls, user.timezone, day)
        events_txt = format_events(events, user.timezone)

        log = get_or_create_log(db, user, day)
        planned_ids = json.loads(log.planned_task_ids_json or "[]")
        completed_ids = set(json.loads(log.completed_task_ids_json or "[]"))

        plan_txt = "You haven't picked anything for today yet — it'll ask at your morning ping, " \
                   "or just text me something now."
        if planned_ids:
            try:
                all_tasks = {t.id: t for t in list_active_tasks()}
            except TodoistError:
                all_tasks = {}
            lines = []
            for tid in planned_ids:
                content = all_tasks[tid].content if tid in all_tasks else "(task)"
                mark = "✅" if tid in completed_ids else "⬜"
                lines.append(f"{mark} {content}")
            plan_txt = "\n".join(lines)

        parts = [f"📅 Calendar:\n{events_txt}", f"\n📋 Today's plan:\n{plan_txt}"]

        if user.people_enabled:
            due = people_svc.people_due_today(db, user, user.timezone)
            if due:
                lines = "\n".join(f"- {people_svc.format_person_line(p, day)}" for p in due)
                parts.append(f"\n👥 People due:\n{lines}")

        if user.meals_enabled:
            statuses = meals_svc.today_meal_status(db, user, day)
            parts.append(f"\n🍽️ Meals:\n{meals_svc.format_meal_summary(user, statuses)}")

        parts.append(f"\n{format_streak_line(db, user, day)}")

        await update.message.reply_text("\n".join(parts))
    finally:
        db.close()


async def tasks_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = str(update.effective_chat.id)
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.telegram_chat_id == chat_id).one_or_none()
        if not user or user.state != "active":
            await update.message.reply_text("Run /start first.")
            return
        try:
            tasks = list_active_tasks()
        except TodoistError as e:
            await update.message.reply_text(f"Todoist error: {e}")
            return
        text = format_tasks_numbered(tasks, user.timezone)
        kb = keyboards.tasks_list_keyboard(tasks) if tasks else None
        await update.message.reply_text(text, reply_markup=kb)
    finally:
        db.close()


async def people_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = str(update.effective_chat.id)
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.telegram_chat_id == chat_id).one_or_none()
        if not user or user.state != "active":
            await update.message.reply_text("Run /start first.")
            return
        if not user.people_enabled:
            await update.message.reply_text("People-tracking is off. Turn it on in /settings.")
            return

        day = today_in_tz(user.timezone)
        ppl = people_svc.list_people(db, user)
        if not ppl:
            text = "No one saved yet."
        else:
            text = "\n".join(f"- {people_svc.format_person_line(p, day)}" for p in ppl)

        kb_rows = [[InlineKeyboardButton("➕ Add person", callback_data="peopleadd:start")]]
        due = [p for p in ppl if (people_svc.overdue_by(p, day) or -1) >= 0]
        for p in due[:8]:
            kb_rows.append([
                InlineKeyboardButton(f"✅ {p.name}", callback_data=f"person:{p.id}:contacted"),
                InlineKeyboardButton("💤 Snooze", callback_data=f"person:{p.id}:snooze"),
            ])
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb_rows))
    finally:
        db.close()


async def meals_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = str(update.effective_chat.id)
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.telegram_chat_id == chat_id).one_or_none()
        if not user or user.state != "active":
            await update.message.reply_text("Run /start first.")
            return
        if not user.meals_enabled:
            await update.message.reply_text("Meal check-ins are off. Turn them on in /settings.")
            return
        day = today_in_tz(user.timezone)
        statuses = meals_svc.today_meal_status(db, user, day)
        text = meals_svc.format_meal_summary(user, statuses)
        rows = []
        for meal in meals_svc.load_meal_times(user):
            if statuses.get(meal) is None:
                rows.append([
                    InlineKeyboardButton(f"🍽️ {meal.capitalize()}", callback_data=f"meal:{meal}:ate"),
                    InlineKeyboardButton("⏭️ Skip", callback_data=f"meal:{meal}:skip"),
                ])
        kb = InlineKeyboardMarkup(rows) if rows else None
        await update.message.reply_text(text, reply_markup=kb)
    finally:
        db.close()


async def streak_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = str(update.effective_chat.id)
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.telegram_chat_id == chat_id).one_or_none()
        if not user or user.state != "active":
            await update.message.reply_text("Run /start first.")
            return
        day = today_in_tz(user.timezone)
        await update.message.reply_text(format_streak_line(db, user, day))
    finally:
        db.close()


async def settings_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = str(update.effective_chat.id)
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.telegram_chat_id == chat_id).one_or_none()
        if not user or user.state != "active":
            await update.message.reply_text("Run /start first.")
            return

        text = (
            f"Timezone: {user.timezone}\n"
            f"Morning ping: {user.morning_time}\n"
            f"Evening ping: {user.evening_time}\n"
            f"Meals: {'on' if user.meals_enabled else 'off'}\n"
            f"People tracking: {'on' if user.people_enabled else 'off'}"
        )
        rows = [
            [InlineKeyboardButton("Change timezone", callback_data="settings:tz")],
            [InlineKeyboardButton("Change morning time", callback_data="settings:morning")],
            [InlineKeyboardButton("Change evening time", callback_data="settings:evening")],
            [InlineKeyboardButton(
                f"Meals: turn {'off' if user.meals_enabled else 'on'}",
                callback_data="settings:mealstoggle",
            )],
            [InlineKeyboardButton(
                f"People: turn {'off' if user.people_enabled else 'on'}",
                callback_data="settings:peopletoggle",
            )],
            [InlineKeyboardButton("Add/replace calendar link", callback_data="settings:addcal")],
        ]
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(rows))
    finally:
        db.close()


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    print("ERROR:", repr(context.error))


# ── Callback query (button) handler ───────────────────────────────────────────

def _rebuild_plan_keyboard(context, user_id: int):
    sel = context.bot_data.get("plan_selection", {}).get(user_id)
    if not sel:
        return None
    tasks = [TodoistTask(id=tid, content=content) for tid, content in sel["tasks"].items()]
    return keyboards.morning_plan_keyboard(tasks, sel["selected"])


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    data = query.data or ""
    chat_id = str(update.effective_chat.id)

    db = SessionLocal()
    try:
        user, _ = _get_or_create_user(db, chat_id)

        # ── Onboarding ──
        if data.startswith("onbtz:"):
            val = data.split(":", 1)[1]
            if val == "custom":
                user.state = "onb_tz_custom"
                db.commit()
                await query.answer()
                await _send_onboarding_step(update.effective_chat.id, user, context)
                return
            user.timezone = val
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

        # ── Settings ──
        if data == "settings:tz":
            await query.answer()
            await query.message.reply_text("New timezone:", reply_markup=keyboards.tz_picker("settz"))
            return

        if data.startswith("settz:"):
            val = data.split(":", 1)[1]
            if val == "custom":
                context.user_data["awaiting"] = "settings_tz_custom"
                await query.answer()
                await query.message.reply_text("Type your IANA timezone, e.g. Europe/Berlin.")
                return
            user.timezone = val
            db.commit()
            schedule_user_jobs(context.application, user)
            await query.answer("Timezone updated")
            await query.message.reply_text(f"✅ Timezone set to {val}. Your pings are rescheduled for it.")
            return

        if data == "settings:morning":
            await query.answer()
            await query.message.reply_text(
                "New morning time:", reply_markup=keyboards.time_picker("setmorning", ["07:00", "08:00", "09:00"])
            )
            return

        if data.startswith("setmorning:"):
            val = data.split(":", 1)[1]
            if val == "custom":
                context.user_data["awaiting"] = "settings_morning_custom"
                await query.answer()
                await query.message.reply_text("Type a 24h time as HH:MM.")
                return
            user.morning_time = val
            db.commit()
            schedule_user_jobs(context.application, user)
            await query.answer("Updated")
            await query.message.reply_text(f"✅ Morning ping set to {val}.")
            return

        if data == "settings:evening":
            await query.answer()
            await query.message.reply_text(
                "New evening time:", reply_markup=keyboards.time_picker("setevening", ["20:00", "21:00", "22:00"])
            )
            return

        if data.startswith("setevening:"):
            val = data.split(":", 1)[1]
            if val == "custom":
                context.user_data["awaiting"] = "settings_evening_custom"
                await query.answer()
                await query.message.reply_text("Type a 24h time as HH:MM.")
                return
            user.evening_time = val
            db.commit()
            schedule_user_jobs(context.application, user)
            await query.answer("Updated")
            await query.message.reply_text(f"✅ Evening ping set to {val}.")
            return

        if data == "settings:mealstoggle":
            user.meals_enabled = not user.meals_enabled
            db.commit()
            schedule_user_jobs(context.application, user)
            await query.answer()
            await query.message.reply_text(f"Meals: {'on' if user.meals_enabled else 'off'}.")
            return

        if data == "settings:peopletoggle":
            user.people_enabled = not user.people_enabled
            db.commit()
            await query.answer()
            await query.message.reply_text(f"People tracking: {'on' if user.people_enabled else 'off'}.")
            return

        if data == "settings:addcal":
            context.user_data["awaiting"] = "settings_cal_custom"
            await query.answer()
            await query.message.reply_text("Paste the ICS link.")
            return

        # ── Morning planning ──
        if data.startswith("plantgl:"):
            task_id = data.split(":", 1)[1]
            sel = context.bot_data.setdefault("plan_selection", {}).get(user.id)
            if not sel:
                await query.answer("This planning session expired — try /today.")
                return
            if task_id in sel["selected"]:
                sel["selected"].discard(task_id)
            else:
                if len(sel["selected"]) >= 5:
                    await query.answer("Max 5 for today — small is the point.")
                    return
                sel["selected"].add(task_id)
            await query.answer()
            await query.edit_message_reply_markup(reply_markup=_rebuild_plan_keyboard(context, user.id))
            return

        if data == "planconfirm":
            sel = context.bot_data.get("plan_selection", {}).get(user.id)
            selected = list(sel["selected"]) if sel else []
            day = today_in_tz(user.timezone)
            log = get_or_create_log(db, user, day)
            log.planned_task_ids_json = json.dumps(selected)
            log.morning_responded_at = datetime.utcnow()
            db.commit()
            mark_nudge_responded(db, user, day, "morning")
            context.bot_data.get("plan_selection", {}).pop(user.id, None)
            await query.answer("Plan set")
            names = [sel["tasks"][t] for t in selected] if sel else []
            summary = "\n".join(f"- {n}" for n in names) or "(nothing picked, but that's logged too)"
            await query.edit_message_text(f"✅ Today's plan:\n{summary}")
            return

        if data == "planskip":
            day = today_in_tz(user.timezone)
            log = get_or_create_log(db, user, day)
            log.morning_responded_at = datetime.utcnow()
            db.commit()
            mark_nudge_responded(db, user, day, "morning")
            context.bot_data.get("plan_selection", {}).pop(user.id, None)
            await query.answer("Skipped")
            await query.edit_message_text("😴 Skipped for today — no judgment. Evening check-in still comes.")
            return

        # ── Task done ──
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
            await query.answer("✅ Done in Todoist")
            kb = query.message.reply_markup
            if kb:
                new_rows = [row for row in kb.inline_keyboard
                            if not any(btn.callback_data == data for btn in row)]
                await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(new_rows) if new_rows else None)
            return

        # ── Meals ──
        if data.startswith("meal:"):
            _, meal, action = data.split(":", 2)
            day = today_in_tz(user.timezone)
            status = "logged" if action == "ate" else "skipped"
            meals_svc.log_meal(db, user, day, meal, status)
            mark_nudge_responded(db, user, day, "meal", meal)
            await query.answer("Logged")
            await query.edit_message_text(f"{'✅' if status == 'logged' else '⏭️'} {meal.capitalize()} logged.")
            return

        # ── People ──
        if data.startswith("person:"):
            _, pid_str, action = data.split(":", 2)
            p = people_svc.get_by_id(db, user, int(pid_str))
            if not p:
                await query.answer("Not found")
                return
            if action == "contacted":
                people_svc.mark_contacted_obj(db, p, user.timezone)
                await query.answer("Logged")
                await query.message.reply_text(f"✅ Logged contact with {p.name}.")
            else:
                people_svc.snooze_obj(db, p, user.timezone, days=3)
                await query.answer("Snoozed 3d")
                await query.message.reply_text(f"💤 Snoozed {p.name} for 3 days.")
            return

        if data == "peopleadd:start":
            context.user_data["awaiting"] = "people_add_name"
            await query.answer()
            await query.message.reply_text("Name?")
            return

        if data == "eveningdone":
            day = today_in_tz(user.timezone)
            log = get_or_create_log(db, user, day)
            log.evening_responded_at = datetime.utcnow()
            db.commit()
            mark_nudge_responded(db, user, day, "evening")
            await query.answer("Noted")
            await query.edit_message_text("✅ Noted. Rest well.")
            return

        await query.answer()
    finally:
        db.close()


# ── Free text: onboarding custom entries, guided flows, else quick-capture ────

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (update.message.text or "").strip()
    if not text:
        return
    chat_id = str(update.effective_chat.id)

    db = SessionLocal()
    try:
        user, created = _get_or_create_user(db, chat_id)

        if user.state != "active":
            await _handle_onboarding_text(update, context, db, user, text)
            return

        awaiting = context.user_data.get("awaiting")
        if awaiting:
            await _handle_awaiting_text(update, context, db, user, awaiting, text)
            return

        # If there's an unanswered evening check-in today, resolve it — but still capture the text.
        day = today_in_tz(user.timezone)
        log = get_or_create_log(db, user, day)
        if log.evening_prompted_at is not None and log.evening_responded_at is None:
            log.evening_responded_at = datetime.utcnow()
            db.commit()
            mark_nudge_responded(db, user, day, "evening")

        try:
            task = quick_capture(db, user, text)
            await update.message.reply_text(f"📥 Captured: {task.content}")
        except TodoistError as e:
            await update.message.reply_text(f"Couldn't capture that — Todoist error: {e}")
    finally:
        db.close()


async def _handle_onboarding_text(update, context, db, user: User, text: str) -> None:
    step = user.state
    chat_id = update.effective_chat.id

    if step == "onb_tz_custom":
        if not valid_timezone(text):
            await update.message.reply_text("That doesn't look like a valid IANA timezone. Try e.g. Europe/Berlin.")
            return
        user.timezone = text
        user.state = "onb_morning"
        db.commit()
        await _send_onboarding_step(chat_id, user, context)
        return

    if step == "onb_morning_custom":
        try:
            t = parse_hhmm(text)
        except Exception:
            await update.message.reply_text("Use 24h HH:MM, e.g. 07:30.")
            return
        user.morning_time = f"{t.hour:02d}:{t.minute:02d}"
        user.state = "onb_evening"
        db.commit()
        await _send_onboarding_step(chat_id, user, context)
        return

    if step == "onb_evening_custom":
        try:
            t = parse_hhmm(text)
        except Exception:
            await update.message.reply_text("Use 24h HH:MM, e.g. 21:30.")
            return
        user.evening_time = f"{t.hour:02d}:{t.minute:02d}"
        user.state = "onb_meals"
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

    # Any other onboarding state: nudge them to use the buttons above.
    await update.message.reply_text("Tap one of the buttons above to continue setup.")


async def _handle_awaiting_text(update, context, db, user: User, awaiting: str, text: str) -> None:
    if awaiting == "settings_tz_custom":
        if not valid_timezone(text):
            await update.message.reply_text("Not a valid IANA timezone. Try e.g. Europe/Berlin.")
            return
        user.timezone = text
        db.commit()
        schedule_user_jobs(context.application, user)
        context.user_data.pop("awaiting", None)
        await update.message.reply_text(f"✅ Timezone set to {text}.")
        return

    if awaiting in ("settings_morning_custom", "settings_evening_custom"):
        try:
            t = parse_hhmm(text)
        except Exception:
            await update.message.reply_text("Use 24h HH:MM, e.g. 07:30.")
            return
        hhmm = f"{t.hour:02d}:{t.minute:02d}"
        if awaiting == "settings_morning_custom":
            user.morning_time = hhmm
        else:
            user.evening_time = hhmm
        db.commit()
        schedule_user_jobs(context.application, user)
        context.user_data.pop("awaiting", None)
        await update.message.reply_text(f"✅ Set to {hhmm}.")
        return

    if awaiting == "settings_cal_custom":
        urls = json.loads(user.gcal_ics_urls_json or "{}")
        urls["primary"] = text
        user.gcal_ics_urls_json = json.dumps(urls)
        db.commit()
        context.user_data.pop("awaiting", None)
        await update.message.reply_text("✅ Calendar link saved.")
        return

    if awaiting == "people_add_name":
        context.user_data["awaiting"] = "people_add_cadence"
        context.user_data["people_add_name"] = text
        await update.message.reply_text("Remind you to reach out every how many days? (type a number, or 'never')")
        return

    if awaiting == "people_add_cadence":
        cadence = None
        if text.strip().lower() not in ("never", "no", "n/a"):
            try:
                cadence = int(text.strip())
            except ValueError:
                await update.message.reply_text("Type a number of days, or 'never'.")
                return
        context.user_data["people_add_cadence"] = cadence
        context.user_data["awaiting"] = "people_add_context"
        await update.message.reply_text("Anything to remember about them? (or type 'skip')")
        return

    if awaiting == "people_add_context":
        name = context.user_data.pop("people_add_name")
        cadence = context.user_data.pop("people_add_cadence")
        context.user_data.pop("awaiting", None)
        note = "" if text.strip().lower() == "skip" else text.strip()
        people_svc.add_person(db, user, name, cadence, note)
        await update.message.reply_text(f"✅ Saved {name}.")
        return

    context.user_data.pop("awaiting", None)


# ── Photo + voice ──────────────────────────────────────────────────────────────

async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = str(update.effective_chat.id)
    caption = (update.message.caption or "").strip()

    db = SessionLocal()
    try:
        user, _ = _get_or_create_user(db, chat_id)
        if user.state != "active" or not user.meals_enabled:
            await update.message.reply_text("📷 Got it — nothing was waiting on a photo right now.")
            return

        day = today_in_tz(user.timezone)
        from .db import PendingNudge
        pending = (
            db.query(PendingNudge)
            .filter(PendingNudge.user_id == user.id, PendingNudge.day == day,
                    PendingNudge.kind == "meal", PendingNudge.responded_at.is_(None))
            .order_by(PendingNudge.last_sent_at.desc())
            .first()
        )
        if not pending:
            await update.message.reply_text("📷 Got it — nothing was waiting on a photo right now.")
            return

        photo = update.message.photo[-1]
        meals_svc.log_meal(db, user, day, pending.ref, "logged", note=caption or None, photo_file_id=photo.file_id)
        mark_nudge_responded(db, user, day, "meal", pending.ref)
        await update.message.reply_text(f"📷 Logged for {pending.ref}.")
    finally:
        db.close()


async def on_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not voice_svc.voice_enabled():
        await update.message.reply_text("Voice capture isn't configured yet (needs OPENAI_API_KEY) — text it for now.")
        return

    tg_file = await update.message.voice.get_file()
    audio_bytes = bytes(await tg_file.download_as_bytearray())
    try:
        text = voice_svc.transcribe_ogg(audio_bytes)
    except Exception as e:
        await update.message.reply_text(f"Couldn't transcribe that: {e}")
        return

    if not text:
        await update.message.reply_text("Didn't catch anything in that voice note.")
        return

    chat_id = str(update.effective_chat.id)
    db = SessionLocal()
    try:
        user, _ = _get_or_create_user(db, chat_id)
        if user.state != "active":
            await update.message.reply_text("Finish /start setup first, then voice capture will work.")
            return
        task = quick_capture(db, user, text)
        await update.message.reply_text(f"🎙️→📥 \"{text}\"\nCaptured: {task.content}")
    except TodoistError as e:
        await update.message.reply_text(f"Transcribed \"{text}\" but couldn't capture it — Todoist error: {e}")
    finally:
        db.close()


# ── Entry point ────────────────────────────────────────────────────────────────

async def _post_init(application: Application) -> None:
    schedule_all_active_users(application)


def main() -> None:
    if BOT_INSTANCE_LOCK == "1":
        lock_path = "/tmp/tg_bot.lock"
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
        except FileExistsError:
            print("Bot already running (lock exists). Exiting.")
            sys.exit(0)

    init_db()

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(_post_init).build()

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("today", today_cmd))
    app.add_handler(CommandHandler("tasks", tasks_cmd))
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
