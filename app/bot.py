from __future__ import annotations

import os
import sys
from datetime import datetime, date

import pytz
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

from .config import TELEGRAM_BOT_TOKEN, STORE_PHOTO_FILE_ID, ALLOWED_MISSES_PER_DAY, BOT_INSTANCE_LOCK
from .db import init_db, SessionLocal, User, Checkin
from .commands import handle_text_command, help_text
from .services.streaks import compute_day_status, compute_streak, format_streak_line
from .services.timeutil import today_in_tz
from .scheduler import start_scheduler


# Helpers

def _utcnow() -> datetime:
    return datetime.utcnow()


def _get_user(db, chat_id: str) -> User | None:
    return db.query(User).filter(User.telegram_chat_id == chat_id).one_or_none()


def _today_user(user: User) -> date:
    return today_in_tz(user.timezone)


def _now_local(user: User) -> datetime:
    tz = pytz.timezone(user.timezone)
    return pytz.utc.localize(_utcnow()).astimezone(tz)


def _pending_daily_checkin(db, user: User, day: date) -> Checkin | None:
    """
    Latest unresponded daily checkin (morning/run/winddown) for today.
    """
    return (
        db.query(Checkin)
        .filter(
            Checkin.user_id == user.id,
            Checkin.day == day,
            Checkin.kind == "daily",
            Checkin.responded_at.is_(None),
        )
        .order_by(Checkin.prompted_at.desc())
        .first()
    )


def _pending_event_checkin(db, user: User, day: date) -> Checkin | None:
    """
    Latest unresponded event checkin for today.
    """
    return (
        db.query(Checkin)
        .filter(
            Checkin.user_id == user.id,
            Checkin.day == day,
            Checkin.kind == "event",
            Checkin.responded_at.is_(None),
        )
        .order_by(Checkin.prompted_at.desc())
        .first()
    )


# Handlers

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = str(update.effective_chat.id)
    _ = handle_text_command(chat_id, "HELP")
    await update.message.reply_text("✅ Connected. Send /help for commands.")

async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    # logs stacktrace automatically; optionally ping you in chat
    try:
        err = context.error
        print("ERROR:", repr(err))
    except Exception:
        pass


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(help_text() + "\n\nExtras:\n/status\n/streak")


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = str(update.effective_chat.id)
    db = SessionLocal()
    try:
        user = _get_user(db, chat_id)
        if not user:
            await update.message.reply_text("Run /start first.")
            return

        day = _today_user(user)
        st = compute_day_status(db, user, day, allowed_misses=ALLOWED_MISSES_PER_DAY)
        cur, best, in_progress = compute_streak(db, user, day, allowed_misses=ALLOWED_MISSES_PER_DAY)

        icon = "✅" if st["honored"] else "⏳"
        msg = (
            f"{icon} {st['completed_total']}/{st['required_total']}  {format_streak_line(cur, best, in_progress)}\n\n"
            f"daily: {st['completed_daily']}/{st['required_daily']}  |  "
            f"events: {st['completed_event_photos']}/{st['required_events']}\n"
            f"misses: {st['misses']} (allowed {st['allowed_misses']})"
        )
        await update.message.reply_text(msg)
    finally:
        db.close()


async def streak_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = str(update.effective_chat.id)
    db = SessionLocal()
    try:
        user = _get_user(db, chat_id)
        if not user:
            await update.message.reply_text("Run /start first.")
            return

        day = _today_user(user)
        cur, best, in_progress = compute_streak(db, user, day, allowed_misses=ALLOWED_MISSES_PER_DAY)
        await update.message.reply_text(format_streak_line(cur, best, in_progress))
    finally:
        db.close()


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = str(update.effective_chat.id)
    text = (update.message.text or "").strip()
    if not text:
        return

    db = SessionLocal()
    try:
        user = _get_user(db, chat_id)
        # Allow /start to create user via handle_text_command, but for plain text we want a user
        if not user:
            # let command handler create it
            resp = handle_text_command(chat_id, text)
            await update.message.reply_text(resp)
            return

        day = _today_user(user)

        # If this looks like a command, run it (commands.py already handles HELP/ACMD/etc)
        # We treat messages that start with common command prefixes as commands.
        # (You can expand this list later.)
        looks_like_cmd = any(
            text.upper().startswith(prefix)
            for prefix in ("PERSON", "EVENTS", "TODO", "TZ", "ACMD", "HELP", "COMMANDS", "DONE")
        )
        if looks_like_cmd:
            resp = handle_text_command(chat_id, text)
            await update.message.reply_text(resp)
            return

        # Otherwise: treat as an ACK to the latest pending daily checkin if one exists today.
        pending_daily = _pending_daily_checkin(db, user, day)
        if pending_daily:
            pending_daily.responded_at = _utcnow()
            pending_daily.response_text = text
            db.commit()
            await update.message.reply_text("✅ Noted.")
            return

        # If no pending daily, just store as note via your handler (it saves notes)
        resp = handle_text_command(chat_id, text)
        await update.message.reply_text(resp)

    finally:
        db.close()


def _pending_meal_checkin(db, user: User, day: date) -> Checkin | None:
    """Latest unresponded meal checkin for today."""
    return (
        db.query(Checkin)
        .filter(
            Checkin.user_id == user.id,
            Checkin.day == day,
            Checkin.kind == "meal",
            Checkin.responded_at.is_(None),
        )
        .order_by(Checkin.prompted_at.desc())
        .first()
    )


async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Attach photo to most recent pending EVENT or MEAL checkin for today.
    Event checkins take priority. Store caption always; store file_id only if STORE_PHOTO_FILE_ID=1.
    """
    chat_id = str(update.effective_chat.id)
    caption = (update.message.caption or "").strip()

    db = SessionLocal()
    try:
        user = _get_user(db, chat_id)
        if not user:
            await update.message.reply_text("Run /start first.")
            return

        day = _today_user(user)

        pending = _pending_event_checkin(db, user, day) or _pending_meal_checkin(db, user, day)
        if not pending:
            await update.message.reply_text("📷 Got it — but there’s no pending check-in right now. Photo not saved to a check-in.")
            return

        photo = update.message.photo[-1]
        file_id = photo.file_id

        pending.responded_at = _utcnow()
        pending.response_text = caption if caption else "(photo)"
        pending.photo_file_id = file_id if STORE_PHOTO_FILE_ID else None

        db.commit()

        label = "event" if pending.kind == "event" else f"{pending.ref} meal"
        await update.message.reply_text(f"✅ Photo logged for {label} check-in.")

    finally:
        db.close()


def main() -> None:
    # Prevent multiple pollers in same container
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

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("streak", streak_cmd))

    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    app.add_error_handler(on_error)

    start_scheduler(app)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
