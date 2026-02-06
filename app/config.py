import os
import json
from pathlib import Path
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///schedulerbot.db")
DEFAULT_TIMEZONE = os.environ.get("DEFAULT_TIMEZONE", "America/New_York")
GCAL_ICS_URLS_JSON = json.loads(os.environ.get("GCAL_ICS_URLS_JSON", "{}"))
TODOIST_API_TOKEN = os.environ.get("TODOIST_API_TOKEN", "")
TODOIST_PROJECT_ID = os.environ.get("TODOIST_PROJECT_ID")
ALLOWED_MISSES_PER_DAY = int(os.environ.get("ALLOWED_MISSES_PER_DAY", "1"))
MEAL_TIMES_JSON = os.environ.get("MEAL_TIMES_JSON", "").strip()
BOT_INSTANCE_LOCK = os.environ.get("BOT_INSTANCE_LOCK", "1")
STORE_PHOTO_FILE_ID = os.environ.get("STORE_PHOTO_FILE_ID", "1")
TEST_SCHEDULE = os.environ.get("TEST_SCHEDULE", "0")