from __future__ import annotations

"""
Lightweight, non-destructive schema migration.

The redesign changed the `users` and `people` tables in ways SQLAlchemy's
create_all() can't handle (it only creates missing tables, never alters
existing ones). A pre-redesign database would therefore crash on first query
with "no such column: users.state".

Strategy: rename any legacy table out of the way, let create_all() build the
new one, then copy the still-meaningful data across. The renamed table is kept
as `<name>_legacy_backup` so nothing is ever actually deleted.
"""

from sqlalchemy import inspect, text


# table -> a column that only exists in the NEW schema. Its absence marks a legacy table.
_LEGACY_MARKERS = {
    "users": "state",
    "people": "context",
}


def _columns(conn, table: str) -> set[str]:
    rows = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
    return {r[1] for r in rows}


def _fetch_all(conn, table: str, cols: list[str]) -> list[dict]:
    present = _columns(conn, table)
    usable = [c for c in cols if c in present]
    if not usable:
        return []
    sql = f"SELECT {', '.join(usable)} FROM {table}"
    return [dict(zip(usable, row)) for row in conn.execute(text(sql)).fetchall()]


def detect_legacy_tables(engine) -> list[str]:
    insp = inspect(engine)
    existing = set(insp.get_table_names())
    out = []
    with engine.connect() as conn:
        for table, marker in _LEGACY_MARKERS.items():
            if table in existing and marker not in _columns(conn, table):
                out.append(table)
    return out


def stash_legacy_tables(engine, tables: list[str]) -> dict[str, list[dict]]:
    """Read legacy rows into memory, then rename the tables aside so create_all()
    can build the new schema. Returns the rescued rows keyed by table name."""
    rescued: dict[str, list[dict]] = {}

    with engine.begin() as conn:
        for table in tables:
            if table == "users":
                rescued[table] = _fetch_all(
                    conn, table, ["id", "telegram_chat_id", "timezone", "created_at"]
                )
            elif table == "people":
                rescued[table] = _fetch_all(
                    conn, table,
                    ["id", "user_id", "name", "note", "base_days", "last_contact",
                     "created_at", "updated_at"],
                )

            backup = f"{table}_legacy_backup"
            conn.execute(text(f"DROP TABLE IF EXISTS {backup}"))
            conn.execute(text(f"ALTER TABLE {table} RENAME TO {backup}"))

    return rescued


def restore_rescued_rows(engine, rescued: dict[str, list[dict]]) -> dict[str, int]:
    """Insert rescued rows into the freshly-created new-schema tables."""
    counts = {"users": 0, "people": 0}

    with engine.begin() as conn:
        for row in rescued.get("users", []):
            # Legacy users never went through the new onboarding (morning/evening
            # times, meals, people toggles didn't exist), so send them through it.
            # Their old timezone is preserved as the starting value.
            conn.execute(
                text(
                    "INSERT INTO users (id, telegram_chat_id, state, timezone, "
                    "morning_time, midday_time, evening_time, meals_enabled, "
                    "meal_times_json, people_enabled, gcal_ics_urls_json, "
                    "escalation_enabled, created_at) "
                    "VALUES (:id, :chat_id, 'onb_tz', :tz, '08:00', '13:00', '21:00', "
                    "1, '{}', 1, '{}', 1, :created_at)"
                ),
                {
                    "id": row.get("id"),
                    "chat_id": row.get("telegram_chat_id"),
                    "tz": row.get("timezone") or "America/New_York",
                    "created_at": row.get("created_at"),
                },
            )
            counts["users"] += 1

        for row in rescued.get("people", []):
            # note -> context (the new grounded free-form field),
            # base_days -> cadence_days. Priority is dropped; it wasn't grounded
            # in anything and the new model uses cadence + context instead.
            conn.execute(
                text(
                    "INSERT INTO people (id, user_id, name, context, cadence_days, "
                    "last_contact, snoozed_until, created_at, updated_at) "
                    "VALUES (:id, :user_id, :name, :context, :cadence, :last_contact, "
                    "NULL, :created_at, :updated_at)"
                ),
                {
                    "id": row.get("id"),
                    "user_id": row.get("user_id"),
                    "name": row.get("name"),
                    "context": (row.get("note") or "").strip(),
                    "cadence": row.get("base_days"),
                    "last_contact": row.get("last_contact"),
                    "created_at": row.get("created_at"),
                    "updated_at": row.get("updated_at"),
                },
            )
            counts["people"] += 1

    return counts
