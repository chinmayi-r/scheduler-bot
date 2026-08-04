from __future__ import annotations

from datetime import datetime
from sqlalchemy import (
    create_engine, Column, Integer, String, DateTime, Date, Boolean,
    ForeignKey, UniqueConstraint, Text
)
from sqlalchemy.orm import declarative_base, sessionmaker, relationship

from .config import DATABASE_URL, DEFAULT_TIMEZONE

Base = declarative_base()
engine = create_engine(DATABASE_URL, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    telegram_chat_id = Column(String, unique=True, nullable=False)

    # Conversation state machine: "onb_tz" | "onb_morning" | ... | "active" | "awaiting:<thing>"
    state = Column(String, nullable=False, default="onb_tz")

    timezone = Column(String, nullable=False, default=DEFAULT_TIMEZONE)

    # Personalized schedule (local HH:MM strings), set during onboarding / /settings.
    morning_time = Column(String, nullable=False, default="08:00")
    midday_time = Column(String, nullable=False, default="13:00")
    evening_time = Column(String, nullable=False, default="21:00")

    meals_enabled = Column(Boolean, nullable=False, default=True)
    meal_times_json = Column(Text, nullable=False, default="{}")  # {"breakfast":"08:30",...}

    people_enabled = Column(Boolean, nullable=False, default=True)

    # {"label": "https://.../basic.ics", ...} - configurable via chat, no redeploy needed.
    gcal_ics_urls_json = Column(Text, nullable=False, default="{}")

    todoist_inbox_project_id = Column(String, nullable=True)

    escalation_enabled = Column(Boolean, nullable=False, default=True)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    people = relationship("Person", back_populates="user", cascade="all, delete-orphan")


class DailyLog(Base):
    """
    One row per user per local day. Tracks whether the day was "engaged" for
    streak purposes, and which Todoist task ids were picked/completed that day.
    """
    __tablename__ = "daily_log"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    day = Column(Date, nullable=False)

    morning_prompted_at = Column(DateTime, nullable=True)
    morning_responded_at = Column(DateTime, nullable=True)

    planned_task_ids_json = Column(Text, nullable=False, default="[]")
    completed_task_ids_json = Column(Text, nullable=False, default="[]")

    midday_prompted_at = Column(DateTime, nullable=True)

    evening_prompted_at = Column(DateTime, nullable=True)
    evening_responded_at = Column(DateTime, nullable=True)

    __table_args__ = (UniqueConstraint("user_id", "day", name="uq_daily_log"),)


class PendingNudge(Base):
    """
    Tracks a sent prompt for dedupe + escalation (re-ping if ignored).
    kind: 'morning' | 'midday' | 'evening' | 'meal' | 'person'
    ref:  '' for daily prompts, meal name, or person id (as string)
    """
    __tablename__ = "pending_nudges"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)

    day = Column(Date, nullable=False)
    kind = Column(String, nullable=False)
    ref = Column(String, nullable=False, default="")

    first_sent_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    last_sent_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    escalation_count = Column(Integer, nullable=False, default=0)

    responded_at = Column(DateTime, nullable=True)

    __table_args__ = (UniqueConstraint("user_id", "day", "kind", "ref", name="uq_pending_nudge"),)


class Person(Base):
    """
    A grounded contact: not just a countdown timer, but running notes on who
    they are / what you know, so a reminder actually gives you context.
    """
    __tablename__ = "people"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)

    name = Column(String, nullable=False)
    context = Column(Text, nullable=False, default="")  # freeform, appended over time
    cadence_days = Column(Integer, nullable=True)  # reach out every N days; null = no reminder
    last_contact = Column(Date, nullable=True)
    snoozed_until = Column(Date, nullable=True)  # "remind me later" without falsely marking as contacted

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    user = relationship("User", back_populates="people")

    __table_args__ = (UniqueConstraint("user_id", "name", name="uq_people_user_name"),)


class MealLog(Base):
    __tablename__ = "meal_log"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)

    day = Column(Date, nullable=False)
    meal = Column(String, nullable=False)  # e.g. "breakfast"
    status = Column(String, nullable=False, default="logged")  # 'logged' | 'skipped'
    note = Column(Text, nullable=True)
    photo_file_id = Column(String, nullable=True)

    logged_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (UniqueConstraint("user_id", "day", "meal", name="uq_meal_log"),)


def init_db() -> None:
    Base.metadata.create_all(bind=engine)
