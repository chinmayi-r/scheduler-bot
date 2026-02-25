from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

import requests

from .. import config  # app/config.py load_dotenv() runs there


TODOIST_API_BASE = "https://api.todoist.com/api/v1"


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
        raise TodoistError("TODOIST_API_TOKEN is not set (or not loaded). Check .env and config.py load_dotenv().")
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
    Cursor-based pagination. :contentReference[oaicite:1]{index=1}
    """
    if limit <= 0 or limit > 200:
        limit = 200

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
    """
    v1: POST /api/v1/tasks/{task_id}/close :contentReference[oaicite:2]{index=2}
    """
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

    out: list[TodoistProject] = []
    for p in r.json().get("results", r.json()):  # tolerate either shape
        out.append(TodoistProject(id=str(p.get("id", "")), name=str(p.get("name", ""))))
    return out


def default_project_id() -> Optional[str]:
    """
    Keep using TODOIST_PROJECT_ID if you set it.
    In v1, project_id can be string IDs (not necessarily numeric). :contentReference[oaicite:3]{index=3}
    """
    pid = getattr(config, "TODOIST_PROJECT_ID", None)
    if pid is None:
        return None
    pid = str(pid).strip()
    return pid or None