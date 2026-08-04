from __future__ import annotations

from datetime import datetime, date

from ..db import Person, User
from .timeutil import today_in_tz


def _find(db, user: User, name: str) -> Person | None:
    return (
        db.query(Person)
        .filter(Person.user_id == user.id, Person.name.ilike(name.strip()))
        .one_or_none()
    )


def add_person(db, user: User, name: str, cadence_days: int | None, context: str = "") -> Person:
    name = name.strip()
    existing = _find(db, user, name)
    if existing:
        return update_context(db, user, name, context) if context else existing

    p = Person(
        user_id=user.id,
        name=name,
        context=context.strip(),
        cadence_days=cadence_days,
        last_contact=None,
    )
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


def update_context(db, user: User, name: str, note: str, tz_name: str | None = None) -> Person | None:
    """Appends a dated note to a person's context — this is what makes the tracker
    'grounded': a reminder later shows what you actually know, not just a countdown."""
    p = _find(db, user, name)
    if not p:
        return None
    day = today_in_tz(tz_name or "UTC")
    stamp = f"[{day.isoformat()}] {note.strip()}"
    p.context = f"{p.context}\n{stamp}".strip() if p.context else stamp
    p.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(p)
    return p


def set_cadence(db, user: User, name: str, cadence_days: int | None) -> Person | None:
    p = _find(db, user, name)
    if not p:
        return None
    p.cadence_days = cadence_days
    p.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(p)
    return p


def get_by_id(db, user: User, person_id: int) -> Person | None:
    return db.query(Person).filter(Person.user_id == user.id, Person.id == person_id).one_or_none()


def mark_contacted(db, user: User, name: str, tz_name: str) -> Person | None:
    p = _find(db, user, name)
    if not p:
        return None
    return mark_contacted_obj(db, p, tz_name)


def mark_contacted_obj(db, p: Person, tz_name: str) -> Person:
    p.last_contact = today_in_tz(tz_name)
    p.snoozed_until = None
    p.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(p)
    return p


def snooze_obj(db, p: Person, tz_name: str, days: int = 3) -> Person:
    from datetime import timedelta
    p.snoozed_until = today_in_tz(tz_name) + timedelta(days=days)
    p.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(p)
    return p


def delete_person(db, user: User, name: str) -> bool:
    p = _find(db, user, name)
    if not p:
        return False
    db.delete(p)
    db.commit()
    return True


def list_people(db, user: User) -> list[Person]:
    return db.query(Person).filter(Person.user_id == user.id).order_by(Person.name.asc()).all()


def overdue_by(p: Person, today: date) -> int | None:
    """None if no cadence set. Otherwise days overdue (0 = due today, negative = not due yet)."""
    if p.cadence_days is None:
        return None
    if p.snoozed_until is not None and today < p.snoozed_until:
        return -1
    ref = p.last_contact
    if ref is None:
        return 9999  # never contacted, always "due"
    return (today - ref).days - p.cadence_days


def people_due_today(db, user: User, tz_name: str) -> list[Person]:
    today = today_in_tz(tz_name)
    due = [p for p in list_people(db, user) if (overdue_by(p, today) or -1) >= 0]
    due.sort(key=lambda p: -(overdue_by(p, today) or 0))
    return due


def format_person_line(p: Person, today: date) -> str:
    ob = overdue_by(p, today)
    if ob is None:
        status = ""
    elif ob >= 9999:
        status = " — never logged as contacted"
    elif ob > 0:
        status = f" — ⚠️ {ob}d overdue"
    elif ob == 0:
        status = " — due today"
    else:
        status = f" — in {-ob}d"
    last_note = ""
    if p.context:
        last_line = p.context.strip().splitlines()[-1]
        last_note = f"\n   ↳ {last_line}"
    return f"{p.name}{status}{last_note}"
