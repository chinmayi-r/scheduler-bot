from __future__ import annotations

"""
Small HTTP listener that runs alongside the polling bot so Pocket can push
extracted action items in. Railway exposes $PORT publicly, so the endpoint is
reachable without any extra infrastructure.
"""

import json

from aiohttp import web

from .config import POCKET_WEBHOOK_SECRET, POCKET_AUTO_CREATE, PORT
from .db import SessionLocal, User, InboxSuggestion, WebhookLog
from .services import pocket as pocket_svc
from .services.capture import quick_capture
from .services.todoist import TodoistError
from . import keyboards

MAX_RAW_CHARS = 4000
KEEP_LOGS = 20


async def _health(request: web.Request) -> web.Response:
    return web.json_response({"ok": True})


def _record_delivery(raw: bytes, *, ok: bool, note: str, items_found: int = 0) -> None:
    """Keeps a short trail of what actually arrived, so a silent failure can be
    diagnosed from the chat instead of guessed at."""
    db = SessionLocal()
    try:
        try:
            body = raw.decode("utf-8", errors="replace")
        except Exception:
            body = "<undecodable>"
        db.add(WebhookLog(
            source="pocket", ok=ok, note=note[:200],
            items_found=items_found, raw=body[:MAX_RAW_CHARS],
        ))
        db.commit()

        stale = (
            db.query(WebhookLog)
            .order_by(WebhookLog.received_at.desc())
            .offset(KEEP_LOGS)
            .all()
        )
        for row in stale:
            db.delete(row)
        if stale:
            db.commit()
    except Exception as e:  # logging must never break delivery
        print(f"Webhook log write failed: {e}")
    finally:
        db.close()


def _active_users(db) -> list[User]:
    return db.query(User).filter(User.state == "active").all()


def _already_seen(db, user_id: int, text: str, ref: str | None) -> bool:
    q = db.query(InboxSuggestion).filter(
        InboxSuggestion.user_id == user_id, InboxSuggestion.text == text
    )
    if ref:
        q = q.filter(InboxSuggestion.external_ref == ref)
    return q.first() is not None


async def _pocket_webhook(request: web.Request) -> web.Response:
    raw = await request.read()

    provided = pocket_svc.find_signature_header(dict(request.headers))
    if not pocket_svc.verify_signature(raw, provided, POCKET_WEBHOOK_SECRET):
        print("Pocket webhook: signature verification FAILED -- rejecting.")
        _record_delivery(raw, ok=False, note="signature verification failed")
        return web.json_response({"error": "bad signature"}, status=401)

    try:
        payload = json.loads(raw.decode("utf-8") or "{}")
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        print(f"Pocket webhook: unparseable body: {e}")
        _record_delivery(raw, ok=False, note=f"unparseable body: {e}")
        return web.json_response({"error": "bad json"}, status=400)

    items = pocket_svc.extract_action_items(payload)
    if not items:
        # Not an error -- plenty of Pocket events carry no action items. Logged
        # with the payload shape so a format mismatch is diagnosable.
        shape = pocket_svc.describe_payload(payload)
        print(f"Pocket webhook: no action items found ({shape})")
        _record_delivery(raw, ok=True, note=f"no action items ({shape})")
        return web.json_response({"ok": True, "action_items": 0})

    _record_delivery(raw, ok=True, note="action items extracted", items_found=len(items))

    ref = None
    if isinstance(payload, dict):
        for key in ("recording_id", "recordingId", "id", "event_id"):
            if isinstance(payload.get(key), (str, int)):
                ref = str(payload[key])
                break

    application = request.app["tg_application"]
    delivered = 0

    db = SessionLocal()
    try:
        for user in _active_users(db):
            fresh = [t for t in items if not _already_seen(db, user.id, t, ref)]
            if not fresh:
                continue

            if POCKET_AUTO_CREATE:
                created = []
                for text in fresh:
                    try:
                        quick_capture(db, user, text)
                        created.append(text)
                        db.add(InboxSuggestion(user_id=user.id, text=text, source="pocket",
                                               external_ref=ref, status="added"))
                    except TodoistError as e:
                        print(f"Pocket webhook: capture failed: {e}")
                db.commit()
                if created:
                    listing = "\n".join(f"• {t}" for t in created)
                    await application.bot.send_message(
                        chat_id=int(user.telegram_chat_id),
                        text=f"🎙️ From your Pocket recording — added {len(created)} task(s):\n{listing}",
                    )
                    delivered += len(created)
            else:
                rows = []
                for text in fresh:
                    row = InboxSuggestion(user_id=user.id, text=text, source="pocket",
                                          external_ref=ref, status="pending")
                    db.add(row)
                    rows.append(row)
                db.commit()
                for row in rows:
                    db.refresh(row)

                listing = "\n".join(f"• {r.text}" for r in rows)
                await application.bot.send_message(
                    chat_id=int(user.telegram_chat_id),
                    text=f"🎙️ Pocket picked up {len(rows)} action item(s):\n{listing}",
                    reply_markup=keyboards.suggestions_keyboard(rows),
                )
                delivered += len(rows)
    finally:
        db.close()

    return web.json_response({"ok": True, "action_items": len(items), "delivered": delivered})


def build_app(tg_application) -> web.Application:
    app = web.Application()
    app["tg_application"] = tg_application
    app.router.add_get("/health", _health)
    app.router.add_post("/webhooks/pocket", _pocket_webhook)
    return app


async def start_webhook_server(tg_application) -> web.AppRunner:
    app = build_app(tg_application)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=PORT)
    await site.start()
    print(f"Webhook server listening on :{PORT} (POST /webhooks/pocket)")
    return runner
