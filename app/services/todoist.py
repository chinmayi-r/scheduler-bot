from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

import requests

from .. import config  # uses app/config.py that load_dotenv() loads


TODOIST_API_BASE = "https://api.todoist.com/rest/v2"


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
    # tolerate people putting quotes in .env
    if (tok.startswith('"') and tok.endswith('"')) or (tok.startswith("'") and tok.endswith("'")):
        tok = tok[1:-1].strip()
    return tok


def _token() -> str:
    tok = _clean_token(getattr(config, "TODOIST_API_TOKEN", ""))
    if not tok:
        raise TodoistError("TODOIST_API_TOKEN is not set (or not loaded). Check your .env loading in config.py.")
    return tok


def _headers() -> dict[str, str]:
    # Todoist expects Authorization: Bearer <token>
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
        pid = str(project_id).strip()
        pid_digits = "".join(ch for ch in pid if ch.isdigit())
        if not pid_digits:
            raise TodoistError(f"Invalid project_id: {project_id!r}")
        payload["project_id"] = pid_digits

    if due_string:
        payload["due_string"] = due_string.strip()

    r = requests.post(
        f"{TODOIST_API_BASE}/tasks",
        headers=_headers(),
        json=payload,
        timeout=25,
    )

    if r.status_code >= 400:
        _raise(r, context="add_task", payload=payload)

    j = r.json()
    return TodoistTask(
        id=str(j["id"]),
        content=str(j.get("content", "")),
        priority=j.get("priority"),
        due=j.get("due"),
        url=j.get("url"),
    )



def list_active_tasks(*, project_id: Optional[str] = None) -> list[TodoistTask]:
    params: dict[str, str] = {}

    if project_id:
        pid = str(project_id).strip()
        pid_digits = "".join(ch for ch in pid if ch.isdigit())
        if not pid_digits:
            raise TodoistError(f"Invalid project_id: {project_id!r} (expected numeric id).")
        params["project_id"] = pid_digits

    r = requests.get(f"{TODOIST_API_BASE}/tasks", headers=_headers(), params=params, timeout=25)
    if r.status_code >= 400:
        _raise(r, context="list_active_tasks", payload=params or None)

    out: list[TodoistTask] = []
    for j in r.json():
        out.append(
            TodoistTask(
                id=str(j["id"]),
                content=str(j.get("content", "")),
                priority=j.get("priority"),
                due=j.get("due"),
                url=j.get("url"),
            )
        )
    return out


def close_task(task_id: str) -> None:
    tid = str(task_id).strip()
    if not tid:
        raise TodoistError("task_id is empty.")

    r = requests.post(f"{TODOIST_API_BASE}/tasks/{tid}/close", headers=_headers(), timeout=25)
    # Todoist REST returns 204 on success for /close
    if r.status_code not in (204, 200):
        _raise(r, context="close_task")


def list_projects() -> list[TodoistProject]:
    """
    Useful because Todoist web URLs may be slugs, not numeric IDs.
    """
    r = requests.get(f"{TODOIST_API_BASE}/projects", headers=_headers(), timeout=25)
    if r.status_code >= 400:
        _raise(r, context="list_projects")

    out: list[TodoistProject] = []
    for j in r.json():
        out.append(TodoistProject(id=str(j["id"]), name=str(j.get("name", ""))))
    return out


def default_project_id() -> Optional[str]:
    """
    Read default project id from config if you set TODOIST_PROJECT_ID.
    """
    pid = getattr(config, "TODOIST_PROJECT_ID", None)
    if pid is None:
        return None
    pid = str(pid).strip()
    pid_digits = "".join(ch for ch in pid if ch.isdigit())
    return pid_digits or None
