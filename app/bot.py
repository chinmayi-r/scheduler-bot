from __future__ import annotations

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

from .config import TELEGRAM_BOT_TOKEN
from .db import init_db, SessionLocal, User, Checkin
from .commands import handle_text_command, help_text
from .scheduler import start_scheduler


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = str(update.effective_chat.id)
    _ = handle_text_command(chat_id, "HELP")
    await update.message.reply_text("✅ Connected. Send /help for commands.")


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(help_text())


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = str(update.effective_chat.id)
    text = update.message.text or ""
    resp = handle_text_command(chat_id, text)
    await update.message.reply_text(resp)


async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Attach photo to most recent pending checkin for today (event or meal).
    """
    chat_id = str(update.effective_chat.id)
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.telegram_chat_id == chat_id).one_or_none()
        if not user:
            await update.message.reply_text("Run /start first.")
            return

        # highest resolution photo is last
        photo = update.message.photo[-1]
        file_id = photo.file_id

        # find latest pending checkin (responded_at is null)
        pending = (
            db.query(Checkin)
            .filter(Checkin.user_id == user.id, Checkin.responded_at.is_(None))
            .order_by(Checkin.prompted_at.desc())
            .first()
        )

        if not pending:
            await update.message.reply_text("📷 Got the photo. No pending check-in right now — saved nowhere. (If you want, send a note saying what it was for.)")
            return

        pending.photo_file_id = file_id
        pending.responded_at = __import__("datetime").datetime.utcnow()
        db.commit()

        await update.message.reply_text("✅ Photo saved.")
    finally:
        db.close()


def main() -> None:
    init_db()
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))

    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    start_scheduler(app)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
