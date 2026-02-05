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
    timezone = Column(String, nullable=False, default=DEFAULT_TIMEZONE)

    # Later (OAuth):
    google_refresh_token = Column(Text, nullable=True)
    google_tasks_refresh_token = Column(Text, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    # When user runs EVENTS REFRESH, we flip this and the scheduler will act within 60s
    needs_reschedule = Column(Boolean, default=False, nullable=False)

    people = relationship("Person", back_populates="user", cascade="all, delete-orphan")


class Person(Base):
    __tablename__ = "people"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)

    name = Column(String, nullable=False)
    priority = Column(Integer, nullable=False)  # 1..10
    note = Column(String, nullable=False)      # one-line note

    # day tracking
    start_day = Column(Date, nullable=True)    # local date when tracking started/reset
    base_days = Column(Integer, nullable=True) # user-entered offset, can be negative

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    user = relationship("User", back_populates="people")

    __table_args__ = (UniqueConstraint("user_id", "name", name="uq_people_user_name"),)


class DailyEventIndex(Base):
    __tablename__ = "daily_event_index"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)

    day = Column(Date, nullable=False)                 # local date
    event_number = Column(Integer, nullable=False)     # 1..N
    google_event_id = Column(String, nullable=False)

    title = Column(String, nullable=False)
    start_dt = Column(DateTime, nullable=False)        # UTC (naive or aware; formatter handles both)
    end_dt = Column(DateTime, nullable=False)

    last_refresh_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint("user_id", "day", "event_number", name="uq_eventnum"),
        UniqueConstraint("user_id", "day", "google_event_id", name="uq_eventid"),
    )


class EventDone(Base):
    __tablename__ = "event_done"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    day = Column(Date, nullable=False)
    google_event_id = Column(String, nullable=False)
    done_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (UniqueConstraint("user_id", "day", "google_event_id", name="uq_done"),)


class TodoCache(Base):
    __tablename__ = "todo_cache"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)

    text = Column(String, nullable=False)
    is_done = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class Note(Base):
    """
    Free-form notes you send during the day (anything not recognized as a command).
    """
    __tablename__ = "notes"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)

    day = Column(Date, nullable=False)                  # local day at time of message
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    text = Column(Text, nullable=False)


class Checkin(Base):
    """
    A prompt we sent ("How's it going? Send pic.") and the response (photo/text).
    kind = 'event' or 'meal'
    ref  = event google_event_id OR meal label ('breakfast','fruit','lunch','dinner')
    """
    __tablename__ = "checkins"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)

    day = Column(Date, nullable=False)
    kind = Column(String, nullable=False)               # 'event' | 'meal'
    ref = Column(String, nullable=False)                # event_id or meal label

    prompted_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    # response
    responded_at = Column(DateTime, nullable=True)
    response_text = Column(Text, nullable=True)
    photo_file_id = Column(String, nullable=True)

    __table_args__ = (
        UniqueConstraint("user_id", "day", "kind", "ref", name="uq_checkin"),
    )


def init_db() -> None:
    Base.metadata.create_all(bind=engine)
