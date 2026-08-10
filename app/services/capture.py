from __future__ import annotations

from ..config import TODOIST_PROJECT_ID
from ..db import User
from . import todoist


def capture_project_id(db, user: User) -> str | None:
    """TODOIST_PROJECT_ID wins if you've set it — that's an explicit choice about
    where your tasks live. Only when it's unset do we fall back to the Inbox."""
    if TODOIST_PROJECT_ID:
        return TODOIST_PROJECT_ID

    pid = todoist.get_or_create_inbox_project_id(user.todoist_inbox_project_id)
    if pid != user.todoist_inbox_project_id:
        user.todoist_inbox_project_id = pid
        db.commit()
    return pid


def quick_capture(db, user: User, text: str) -> todoist.TodoistTask:
    """Any free text becomes a real Todoist task instantly — no syntax, no note
    limbo. This is the whole fix for 'I procrastinate writing tasks down'."""
    return todoist.add_task(text, project_id=capture_project_id(db, user))
