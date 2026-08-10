from __future__ import annotations

from ..db import InboxSuggestion, User
from . import pocket as pocket_svc


def pull_action_items(db, user: User) -> tuple[int, int]:
    """Pulls open action items from Pocket into the approval inbox.

    Returns (new, total_seen). De-duplicated on Pocket's own action-item id, so
    polling repeatedly is safe and re-surfacing an item you dismissed doesn't
    happen either.
    """
    items = pocket_svc.search_action_items(status="TODO")

    existing = {
        row.external_ref
        for row in db.query(InboxSuggestion)
        .filter(InboxSuggestion.user_id == user.id, InboxSuggestion.source == "pocket")
        .all()
        if row.external_ref
    }

    new = 0
    for item in items:
        if item.id in existing:
            continue
        text = item.label
        if item.context:
            text = f"{text} — {item.context}"[:400]
        db.add(InboxSuggestion(
            user_id=user.id, text=text, source="pocket",
            external_ref=item.id, status="pending",
        ))
        new += 1

    if new:
        db.commit()
    return new, len(items)


def link_task(db, suggestion: InboxSuggestion, todoist_task_id: str) -> None:
    suggestion.todoist_task_id = todoist_task_id
    db.commit()


def complete_linked_action_item(db, user: User, todoist_task_id: str) -> str | None:
    """Best-effort write-back: finishing a task here closes the Pocket action
    item it came from. Never raises -- a Pocket outage must not break marking
    something done."""
    row = (
        db.query(InboxSuggestion)
        .filter(
            InboxSuggestion.user_id == user.id,
            InboxSuggestion.source == "pocket",
            InboxSuggestion.todoist_task_id == str(todoist_task_id),
        )
        .one_or_none()
    )
    if not row or not row.external_ref:
        return None

    try:
        pocket_svc.complete_action_item(row.external_ref)
        return row.text
    except pocket_svc.PocketError as e:
        print(f"Pocket write-back failed for {row.external_ref}: {e}")
        return None
