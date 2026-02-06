import json
import config


def check(name, value, validator=None):
    print(f"\n--- {name} ---")

    if value is None or value == "":
        print("⚠️  EMPTY / NOT SET")
        return

    print("Value:", value)
    print("Type:", type(value).__name__)

    if validator:
        try:
            validator(value)
            print("✅ Validation passed")
        except Exception as e:
            print("❌ Validation failed:", e)


def validate_json_string(raw):
    json.loads(raw)


def validate_int(val):
    int(val)


def validate_bool_string(val):
    if str(val) not in {"0", "1"}:
        raise ValueError("Must be '0' or '1'")


def main():
    print("========== CONFIG DIAGNOSTICS ==========")

    check("TELEGRAM_BOT_TOKEN", config.TELEGRAM_BOT_TOKEN)

    check("DATABASE_URL", config.DATABASE_URL)

    check("DEFAULT_TIMEZONE", config.DEFAULT_TIMEZONE)

    check("GCAL_ICS_URLS", config.GCAL_ICS_URLS)

    check("TODOIST_API_TOKEN", config.TODOIST_API_TOKEN)

    check("TODOIST_PROJECT_ID", config.TODOIST_PROJECT_ID)

    check("ALLOWED_MISSES_PER_DAY", config.ALLOWED_MISSES_PER_DAY, validate_int)

    # MEAL TIMES
    raw_meal = config.MEAL_TIMES_JSON
    check("MEAL_TIMES_JSON (raw string)", raw_meal, validate_json_string if raw_meal else None)

    if raw_meal:
        try:
            parsed = json.loads(raw_meal)
            print("Parsed meal times:", parsed)
        except Exception:
            pass

    check("BOT_INSTANCE_LOCK", config.BOT_INSTANCE_LOCK, validate_bool_string)

    check("STORE_PHOTO_FILE_ID", config.STORE_PHOTO_FILE_ID, validate_bool_string)

    print("\n========== DONE ==========")


if __name__ == "__main__":
    main()
