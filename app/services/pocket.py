from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any, Iterable

import requests

from ..config import POCKET_API_KEY

POCKET_API_BASE = "https://api.heypocketai.com"

# Pocket signs webhooks with a per-hook secret. The exact header name isn't
# public, so accept the plausible spellings rather than silently rejecting
# every delivery.
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


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {POCKET_API_KEY}", "Content-Type": "application/json"}


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


# ── Payload parsing ───────────────────────────────────────────────────────────

_ITEM_KEYS = ("action_items", "actionItems", "action_item", "actionitems", "tasks", "todos")
_TEXT_KEYS = ("text", "content", "title", "task", "description", "name", "summary")


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
    """Short shape summary, logged when a delivery yields no action items so a
    format mismatch is diagnosable instead of invisible."""
    try:
        if isinstance(payload, dict):
            return f"dict keys={sorted(payload.keys())[:12]}"
        if isinstance(payload, list):
            return f"list len={len(payload)}"
        return type(payload).__name__
    except Exception:
        return "unknown"


# ── Read API (fallback / on-demand pull) ──────────────────────────────────────

def list_recent_recordings(limit: int = 5) -> list[dict]:
    if not pocket_api_enabled():
        raise PocketError("POCKET_API_KEY is not set.")
    try:
        r = requests.get(
            f"{POCKET_API_BASE}/v1/recordings",
            headers=_headers(),
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
