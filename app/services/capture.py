from __future__ import annotations

from ..db import User
from . import todoist


def quick_capture(db, user: User, text: str) -> todoist.TodoistTask:
    """Any free text becomes a real Todoist task instantly — no syntax, no note
    limbo. This is the whole fix for 'I procrastinate writing tasks down'."""
    pid = todoist.get_or_create_inbox_project_id(user.todoist_inbox_project_id)
    if pid != user.todoist_inbox_project_id:
        user.todoist_inbox_project_id = pid
        db.commit()
    return todoist.add_task(text, project_id=pid)
