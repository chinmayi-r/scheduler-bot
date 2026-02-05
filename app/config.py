import os
import json
from pathlib import Path
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///schedulerbot.db")
DEFAULT_TIMEZONE = os.environ.get("DEFAULT_TIMEZONE", "America/New_York")
GCAL_ICS_URLS = json.loads(os.environ.get("GCAL_ICS_URLS_JSON", "{}"))
TODOIST_API_TOKEN = os.environ.get("TODOIST_API_TOKEN", "")
TODOIST_PROJECT_ID = os.environ.get("TODOIST_PROJECT_ID")