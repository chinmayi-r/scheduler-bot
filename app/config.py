"""Every setting the bot has. If something isn't here, it isn't configurable."""

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

# ── Required ──
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TODOIST_API_TOKEN = os.environ.get("TODOIST_API_TOKEN", "")

# Which Todoist project captured tasks go to, and which the bot reads back.
# Unset = your Todoist Inbox.
TODOIST_PROJECT_ID = os.environ.get("TODOIST_PROJECT_ID", "").strip()

# ── Storage ──
# On Railway this MUST point at a mounted volume, or every deploy wipes it:
#   sqlite:////data/schedulerbot.db
DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///schedulerbot.db")

# ── Behaviour ──
# Only used until you pick a timezone during setup.
DEFAULT_TIMEZONE = os.environ.get("DEFAULT_TIMEZONE", "America/New_York")

# Missed days in a row before the streak resets.
# 1 = strict, 2 = one slip forgiven, 3 = looser.
STREAK_BREAK_AFTER_MISSES = int(os.environ.get("STREAK_BREAK_AFTER_MISSES", "2"))

# If you ignore the morning nudge, re-send it this many minutes later, this many
# times. Set NUDGE_REPEATS=0 to never re-send.
NUDGE_REPEAT_MINUTES = int(os.environ.get("NUDGE_REPEAT_MINUTES", "45"))
NUDGE_REPEATS = int(os.environ.get("NUDGE_REPEATS", "2"))
