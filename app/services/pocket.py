from __future__ import annotations

"""
Pocket integration.

Two ways in, because they fail differently:

1. MCP (preferred). Pocket's MCP server exposes `search_pocket_actionitems` and
   `update_pocket_actionitem` on all plans, so action items can be pulled
   directly and completions written back. Needs no public endpoint and no
   guessing at payload shapes.
2. Webhooks (optional). Push delivery, useful for near-instant arrival, but the
   payload format isn't publicly documented -- so parsing there stays tolerant.
"""

import hashlib
import hmac
import json
from dataclasses import dataclass
from typing import Any

import requests

from ..config import POCKET_API_KEY

# Per Pocket's API reference. The REST surface documents recordings/search/tags;
# action items are reached through the MCP server below.
POCKET_API_BASE = "https://public.heypocketai.com/api/v1"
POCKET_MCP_URL = "https://public.heypocketai.com/mcp"

_SIGNATURE_HEADERS = (
    "X-Pocket-Signature",
    "X-Pocket-Signature-256",
    "X-Signature",
    "X-Signature-256",
    "X-Hub-Signature-256",
)


class PocketError(RuntimeError):
    pass


def pocket_api_enabled() -> bool:
    return bool(POCKET_API_KEY)


def _auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {POCKET_API_KEY}"}


# ── Webhook verification ──────────────────────────────────────────────────────

def find_signature_header(headers: dict[str, str]) -> str | None:
    lowered = {k.lower(): v for k, v in headers.items()}
    for name in _SIGNATURE_HEADERS:
        val = lowered.get(name.lower())
        if val:
            return val
    return None


def verify_signature(raw_body: bytes, provided: str | None, secret: str) -> bool:
    """HMAC-SHA256 over the raw body, compared in constant time. Tolerates the
    common `sha256=<hex>` prefix form."""
    if not secret:
        return True  # verification disabled
    if not provided:
        return False

    candidate = provided.strip()
    if "=" in candidate:
        candidate = candidate.split("=", 1)[1].strip()

    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, candidate.lower())


# ── Webhook payload parsing ───────────────────────────────────────────────────

_ITEM_KEYS = ("action_items", "actionItems", "action_item", "actionitems", "tasks", "todos")
_TEXT_KEYS = ("label", "text", "content", "title", "task", "description", "name", "summary")


def _coerce_item(node: Any) -> str | None:
    if isinstance(node, str):
        return node.strip() or None
    if isinstance(node, dict):
        for key in _TEXT_KEYS:
            val = node.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
    return None


def extract_action_items(payload: Any) -> list[str]:
    """Walks the payload looking for action-item collections wherever Pocket
    nests them. Shape-tolerant on purpose: the webhook format isn't publicly
    documented, and a silently-dropped action item is worse than a stray one."""
    found: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, val in node.items():
                if key in _ITEM_KEYS and isinstance(val, list):
                    for entry in val:
                        text = _coerce_item(entry)
                        if text:
                            found.append(text)
                else:
                    walk(val)
        elif isinstance(node, list):
            for entry in node:
                walk(entry)

    walk(payload)

    seen: set[str] = set()
    unique: list[str] = []
    for item in found:
        key = item.lower()
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique


def describe_payload(payload: Any) -> str:
    try:
        if isinstance(payload, dict):
            return f"dict keys={sorted(payload.keys())[:12]}"
        if isinstance(payload, list):
            return f"list len={len(payload)}"
        return type(payload).__name__
    except Exception:
        return "unknown"


# ── Minimal MCP client (streamable HTTP / JSON-RPC) ───────────────────────────

@dataclass
class PocketActionItem:
    id: str
    label: str
    context: str = ""
    status: str = "TODO"
    priority: str = ""
    due_date: str = ""


def _parse_mcp_body(resp: requests.Response) -> dict:
    """Streamable-HTTP servers may answer with plain JSON or an SSE stream, so
    handle both rather than assuming."""
    text = resp.text or ""
    ctype = resp.headers.get("Content-Type", "")

    if "text/event-stream" in ctype or text.lstrip().startswith("event:"):
        for line in text.splitlines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            chunk = line[len("data:"):].strip()
            if not chunk or chunk == "[DONE]":
                continue
            try:
                parsed = json.loads(chunk)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict) and ("result" in parsed or "error" in parsed):
                return parsed
        raise PocketError("no JSON-RPC payload in SSE response")

    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise PocketError(f"unparseable MCP response: {text[:200]}") from e


def _mcp_request(method: str, params: dict | None, session_id: str | None,
                 req_id: int) -> tuple[dict, str | None]:
    headers = {
        **_auth_headers(),
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if session_id:
        headers["Mcp-Session-Id"] = session_id

    body: dict[str, Any] = {"jsonrpc": "2.0", "id": req_id, "method": method}
    if params is not None:
        body["params"] = params

    try:
        resp = requests.post(POCKET_MCP_URL, headers=headers, json=body, timeout=30)
    except requests.RequestException as e:
        raise PocketError(f"could not reach Pocket MCP: {e}") from e

    if resp.status_code == 401:
        raise PocketError("Pocket rejected the API key (401). Check POCKET_API_KEY starts with pk_.")
    if resp.status_code >= 400:
        raise PocketError(f"Pocket MCP {resp.status_code}: {resp.text[:200]}")

    new_session = resp.headers.get("Mcp-Session-Id") or session_id
    parsed = _parse_mcp_body(resp)
    if "error" in parsed and parsed["error"]:
        err = parsed["error"]
        msg = err.get("message") if isinstance(err, dict) else str(err)
        raise PocketError(f"Pocket MCP error: {msg}")
    return parsed.get("result", {}), new_session


def _mcp_call_tool(tool: str, arguments: dict) -> dict:
    if not pocket_api_enabled():
        raise PocketError("POCKET_API_KEY is not set.")

    _, session = _mcp_request(
        "initialize",
        {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "scheduler-bot", "version": "1.0"},
        },
        None,
        1,
    )

    # Best-effort per the MCP handshake; servers that don't need it ignore it.
    try:
        _mcp_request("notifications/initialized", {}, session, 2)
    except PocketError:
        pass

    result, _ = _mcp_request("tools/call", {"name": tool, "arguments": arguments}, session, 3)
    return result


def _payload_from_tool_result(result: dict) -> Any:
    """MCP tool results carry their real payload as JSON inside a text block."""
    if not isinstance(result, dict):
        return result

    if isinstance(result.get("structuredContent"), (dict, list)):
        return result["structuredContent"]

    for block in result.get("content") or []:
        if not isinstance(block, dict):
            continue
        text = block.get("text")
        if not isinstance(text, str):
            continue
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text
    return result


def _iter_item_dicts(payload: Any) -> list[dict]:
    """Finds the action-item records regardless of which envelope key wraps them."""
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        for key in ("data", "results", "items", "actionItems", "action_items"):
            val = payload.get(key)
            if isinstance(val, list):
                return [x for x in val if isinstance(x, dict)]
            if isinstance(val, dict):
                nested = _iter_item_dicts(val)
                if nested:
                    return nested
    return []


def _to_action_item(raw: dict) -> PocketActionItem | None:
    item_id = raw.get("actionItemId") or raw.get("id") or raw.get("action_item_id")
    label = raw.get("label") or raw.get("title") or raw.get("text") or raw.get("content")
    if not item_id or not isinstance(label, str) or not label.strip():
        return None
    return PocketActionItem(
        id=str(item_id),
        label=label.strip(),
        context=str(raw.get("context") or "").strip(),
        status=str(raw.get("status") or "TODO"),
        priority=str(raw.get("priority") or ""),
        due_date=str(raw.get("dueDate") or raw.get("due_date") or ""),
    )


def search_action_items(status: str = "TODO", query: str | None = None) -> list[PocketActionItem]:
    args: dict[str, Any] = {}
    if status:
        args["status"] = status
    if query:
        args["query"] = query

    result = _mcp_call_tool("search_pocket_actionitems", args)
    payload = _payload_from_tool_result(result)

    items = []
    for raw in _iter_item_dicts(payload):
        parsed = _to_action_item(raw)
        if parsed:
            items.append(parsed)
    return items


def complete_action_item(action_item_id: str) -> None:
    """Marks an action item done in Pocket, so finishing it here doesn't leave
    it sitting open over there."""
    _mcp_call_tool(
        "update_pocket_actionitem",
        {"actionItemId": str(action_item_id), "status": "COMPLETED"},
    )


def account_info() -> dict:
    """Cheap connectivity/auth check used by /pocketdebug."""
    result = _mcp_call_tool("get_account_info", {})
    payload = _payload_from_tool_result(result)
    if isinstance(payload, dict):
        return payload.get("data") if isinstance(payload.get("data"), dict) else payload
    return {}


# ── REST (recordings) ─────────────────────────────────────────────────────────

def list_recent_recordings(limit: int = 5) -> list[dict]:
    if not pocket_api_enabled():
        raise PocketError("POCKET_API_KEY is not set.")
    try:
        r = requests.get(
            f"{POCKET_API_BASE}/public/recordings",
            headers=_auth_headers(),
            params={"limit": limit},
            timeout=25,
        )
    except requests.RequestException as e:
        raise PocketError(f"could not reach Pocket: {e}") from e

    if r.status_code >= 400:
        raise PocketError(f"Pocket API {r.status_code}: {r.text[:200]}")

    try:
        body = r.json()
    except json.JSONDecodeError as e:
        raise PocketError(f"Pocket returned non-JSON: {e}") from e

    if isinstance(body, dict):
        for key in ("results", "data", "recordings", "items"):
            if isinstance(body.get(key), list):
                return body[key]
        return []
    return body if isinstance(body, list) else []
