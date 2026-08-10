from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

import requests

from .. import config  # app/config.py load_dotenv() runs there


TODOIST_API_BASE = "https://api.todoist.com/api/v1"
INBOX_PROJECT_NAME = "Inbox"


@dataclass
class TodoistTask:
    id: str
    content: str
    priority: int | None = None
    due: dict | None = None
    url: str | None = None


@dataclass
class TodoistProject:
    id: str
    name: str


class TodoistError(RuntimeError):
    pass


def _clean_token(tok: str) -> str:
    tok = (tok or "").strip()
    if (tok.startswith('"') and tok.endswith('"')) or (tok.startswith("'") and tok.endswith("'")):
        tok = tok[1:-1].strip()
    return tok


def _token() -> str:
    tok = _clean_token(getattr(config, "TODOIST_API_TOKEN", ""))
    if not tok:
        raise TodoistError("TODOIST_API_TOKEN is not set. Add it in your deploy env vars.")
    return tok


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {_token()}",
        "Content-Type": "application/json",
    }


def _raise(r: requests.Response, *, context: str, payload: Optional[dict[str, Any]] = None) -> None:
    try:
        detail = r.json()
    except Exception:
        detail = r.text
    msg = f"Todoist {context} failed: {r.status_code} detail={detail}"
    if payload is not None:
        msg += f" payload={payload}"
    raise TodoistError(msg)


def add_task(
    content: str,
    *,
    project_id: Optional[str] = None,
    due_string: Optional[str] = None,
) -> TodoistTask:
    content = (content or "").strip()
    if not content:
        raise TodoistError("Task content is empty.")

    payload: dict[str, Any] = {"content": content}

    if project_id:
        payload["project_id"] = str(project_id).strip()

    if due_string:
        payload["due_string"] = str(due_string).strip()

    r = requests.post(f"{TODOIST_API_BASE}/tasks", headers=_headers(), json=payload, timeout=25)
    if r.status_code >= 400:
        _raise(r, context="add_task", payload=payload)

    j = r.json()
    return TodoistTask(
        id=str(j.get("id", "")),
        content=str(j.get("content", "")),
        priority=j.get("priority"),
        due=j.get("due"),
        url=j.get("url"),
    )


def list_active_tasks(*, project_id: Optional[str] = None, limit: int = 200) -> list[TodoistTask]:
    """
    v1 GET /api/v1/tasks returns:
      { "results": [...], "next_cursor": "..." }
    Cursor-based pagination.
    """
    if limit <= 0 or limit > 200:
        limit = 200

    # Scope to the configured project by default, so setting TODOIST_PROJECT_ID
    # narrows what the bot shows as well as where it writes -- otherwise the
    # daily prompts would list tasks from every project you own.
    if project_id is None:
        project_id = getattr(config, "TODOIST_PROJECT_ID", "") or None

    params: dict[str, Any] = {"limit": limit}
    if project_id:
        params["project_id"] = str(project_id).strip()

    out: list[TodoistTask] = []
    cursor: Optional[str] = None

    for _page in range(10):  # hard cap to avoid infinite loops
        if cursor:
            params["cursor"] = cursor
        elif "cursor" in params:
            params.pop("cursor", None)

        r = requests.get(f"{TODOIST_API_BASE}/tasks", headers=_headers(), params=params, timeout=25)
        if r.status_code >= 400:
            _raise(r, context="list_active_tasks", payload=params)

        j = r.json()
        results = j.get("results", [])
        for t in results:
            out.append(
                TodoistTask(
                    id=str(t.get("id", "")),
                    content=str(t.get("content", "")),
                    priority=t.get("priority"),
                    due=t.get("due"),
                    url=t.get("url"),
                )
            )

        cursor = j.get("next_cursor")
        if not cursor:
            break

    return out


def close_task(task_id: str) -> None:
    """v1: POST /api/v1/tasks/{task_id}/close"""
    tid = str(task_id).strip()
    if not tid:
        raise TodoistError("task_id is empty.")

    r = requests.post(f"{TODOIST_API_BASE}/tasks/{tid}/close", headers=_headers(), timeout=25)
    if r.status_code >= 400:
        _raise(r, context="close_task")


def list_projects() -> list[TodoistProject]:
    r = requests.get(f"{TODOIST_API_BASE}/projects", headers=_headers(), timeout=25)
    if r.status_code >= 400:
        _raise(r, context="list_projects")

    j = r.json()
    results = j.get("results", j) if isinstance(j, dict) else j
    out: list[TodoistProject] = []
    for p in results:
        out.append(TodoistProject(id=str(p.get("id", "")), name=str(p.get("name", ""))))
    return out


def create_project(name: str) -> TodoistProject:
    r = requests.post(
        f"{TODOIST_API_BASE}/projects",
        headers=_headers(),
        json={"name": name},
        timeout=25,
    )
    if r.status_code >= 400:
        _raise(r, context="create_project", payload={"name": name})
    j = r.json()
    return TodoistProject(id=str(j.get("id", "")), name=str(j.get("name", "")))


def get_or_create_inbox_project_id(cached_id: Optional[str]) -> str:
    """
    Returns a Todoist project id to use as the capture inbox.
    Prefers `cached_id` (already validated once). Otherwise looks for a
    project named "Inbox", creating one if none exists.
    Caller is responsible for persisting the returned id (User.todoist_inbox_project_id).
    """
    if cached_id:
        return cached_id

    for p in list_projects():
        if p.name.strip().lower() == INBOX_PROJECT_NAME.lower():
            return p.id

    created = create_project(INBOX_PROJECT_NAME)
    return created.id


def default_project_id() -> Optional[str]:
    return None
