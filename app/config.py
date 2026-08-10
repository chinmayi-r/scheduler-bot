import os
from pathlib import Path
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///schedulerbot.db")
DEFAULT_TIMEZONE = os.environ.get("DEFAULT_TIMEZONE", "America/New_York")

# Todoist is the single source of truth for tasks (real app, real GUI —
# the bot only orchestrates nudges/capture against it, it doesn't own a task table).
TODOIST_API_TOKEN = os.environ.get("TODOIST_API_TOKEN", "")

# Optional bootstrap calendar sources for the first user during onboarding.
# Per-user calendar URLs are stored in the DB after that (no redeploy needed to change them).
GCAL_ICS_URLS_JSON_DEFAULT = os.environ.get("GCAL_ICS_URLS_JSON", "")

# Voice-note capture. Transcription needs an audio-capable endpoint, so this is
# OpenAI specifically -- OpenRouter doesn't serve the transcription API.
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_TRANSCRIBE_MODEL = os.environ.get("OPENAI_TRANSCRIBE_MODEL", "whisper-1")

# Text LLM for "break this down into micro-steps". Any OpenAI-compatible
# endpoint works -- defaults to OpenRouter's free tier, falls back to the
# OpenAI key if that's all that's set.
LLM_API_KEY = os.environ.get("LLM_API_KEY", "") or os.environ.get("OPENROUTER_API_KEY", "") or OPENAI_API_KEY
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://openrouter.ai/api/v1")
LLM_MODEL = os.environ.get("LLM_MODEL", "meta-llama/llama-3.3-70b-instruct:free")

# Pocket (heypocket.com) action-item sync. When the webhook secret is set, the
# bot serves an HTTP endpoint so Pocket can push extracted action items in.
POCKET_API_KEY = os.environ.get("POCKET_API_KEY", "")
POCKET_WEBHOOK_SECRET = os.environ.get("POCKET_WEBHOOK_SECRET", "")
POCKET_AUTO_CREATE = os.environ.get("POCKET_AUTO_CREATE", "0") == "1"
POCKET_POLL_SECONDS = int(os.environ.get("POCKET_POLL_SECONDS", "900"))
PORT = int(os.environ.get("PORT", "8080"))

BOT_INSTANCE_LOCK = os.environ.get("BOT_INSTANCE_LOCK", "1")

# How many times to re-ping an unanswered prompt before giving up, and how far apart.
ESCALATION_MAX = int(os.environ.get("ESCALATION_MAX", "3"))
ESCALATION_MINUTES = [int(x) for x in os.environ.get("ESCALATION_MINUTES", "10,30,60").split(",") if x.strip()]
