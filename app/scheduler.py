from __future__ import annotations

import json
from datetime import datetime, timedelta, time as dtime

import pytz
from telegram.ext import ContextTypes

from .config import ESCALATION_MAX, ESCALATION_MINUTES, POCKET_POLL_SECONDS
from .db import SessionLocal, User, DailyLog, PendingNudge, InboxSuggestion
from .services.timeutil import today_in_tz, parse_hhmm
from .services.meals import load_meal_times
from .services.streaks import get_or_create_log, format_streak_line, days_since_last_engagement
from .services.people import people_due_today, format_person_line
from .services.todoist import list_active_tasks, TodoistError
from .services.gcal import fetch_events_for_day_multi_ics
from .services.formatters import format_events
from . import keyboards


# ── Job naming ────────────────────────────────────────────────────────────────

def _name(user_id: int, kind: str, ref: str = "") -> str:
    return f"user{user_id}:{kind}:{ref}" if ref else f"user{user_id}:{kind}"


def unschedule_user_jobs(app, user_id: int) -> None:
    for job in app.job_queue.jobs():
        if job.name and job.name.startswith(f"user{user_id}:"):
            job.schedule_removal()


def schedule_user_jobs(app, user: User) -> None:
    """Call whenever a user finishes onboarding or changes timezone/times/toggles.
    Removes all existing jobs for the user and re-adds from current settings —
    this is what makes changing timezone actually take effect immediately."""
    unschedule_user_jobs(app, user.id)

    if user.state != "active":
        return

    tz = pytz.timezone(user.timezone)

    def at(hhmm: str) -> dtime:
        t = parse_hhmm(hhmm)
        return dtime(t.hour, t.minute, tzinfo=tz)

    jq = app.job_queue
    jq.run_daily(_job_morning, time=at(user.morning_time), name=_name(user.id, "morning"),
                 data={"user_id": user.id})
    jq.run_daily(_job_midday, time=at(user.midday_time), name=_name(user.id, "midday"),
                 data={"user_id": user.id})
    jq.run_daily(_job_evening, time=at(user.evening_time), name=_name(user.id, "evening"),
                 data={"user_id": user.id})

    if user.meals_enabled:
        for meal, hhmm in load_meal_times(user).items():
            try:
                t = at(hhmm)
            except Exception:
                continue
            jq.run_daily(_job_meal, time=t, name=_name(user.id, "meal", meal),
                         data={"user_id": user.id, "meal": meal})


def schedule_all_active_users(app) -> None:
    db = SessionLocal()
    try:
        for u in db.query(User).filter(User.state == "active").all():
            schedule_user_jobs(app, u)
    finally:
        db.close()

    from .services.pocket import pocket_api_enabled
    if pocket_api_enabled() and not any(j.name == "pocket_poll" for j in app.job_queue.jobs()):
        app.job_queue.run_repeating(
            _job_pocket_poll, interval=POCKET_POLL_SECONDS, first=30, name="pocket_poll"
        )
        print(f"Pocket polling every {POCKET_POLL_SECONDS}s")


# ── Escalation (re-ping if ignored) ──────────────────────────────────────────

def _get_or_make_nudge(db, user: User, day, kind: str, ref: str = "") -> tuple[PendingNudge, bool]:
    """Returns (nudge, is_new). is_new=False means this was already sent today (dedupe)."""
    row = (
        db.query(PendingNudge)
        .filter(PendingNudge.user_id == user.id, PendingNudge.day == day,
                PendingNudge.kind == kind, PendingNudge.ref == ref)
        .one_or_none()
    )
    if row:
        return row, False
    row = PendingNudge(user_id=user.id, day=day, kind=kind, ref=ref)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row, True


def _schedule_escalation(app, user_id: int, kind: str, ref: str, day_iso: str) -> None:
    if not ESCALATION_MINUTES:
        return
    app.job_queue.run_once(
        _job_escalate,
        when=timedelta(minutes=ESCALATION_MINUTES[0]),
        name=_name(user_id, "escalate", f"{kind}:{ref}"),
        data={"user_id": user_id, "kind": kind, "ref": ref, "day": day_iso, "step": 0},
    )


async def _job_escalate(context: ContextTypes.DEFAULT_TYPE) -> None:
    data = context.job.data
    user_id, kind, ref, day_iso, step = data["user_id"], data["kind"], data["ref"], data["day"], data["step"]

    db = SessionLocal()
    try:
        user = db.get(User, user_id)
        if not user or not user.escalation_enabled:
            return

        from datetime import date as _date
        day = _date.fromisoformat(day_iso)
        row = (
            db.query(PendingNudge)
            .filter(PendingNudge.user_id == user_id, PendingNudge.day == day,
                    PendingNudge.kind == kind, PendingNudge.ref == ref)
            .one_or_none()
        )
        if not row or row.responded_at is not None:
            return  # already handled, stop nagging

        if row.escalation_count >= ESCALATION_MAX:
            return

        # Midday nags about outstanding work, so re-check the work rather than
        # trusting a flag: finishing tasks anywhere (here, or in Todoist itself)
        # should silence it.
        if kind == "midday":
            log = get_or_create_log(db, user, day)
            planned = json.loads(log.planned_task_ids_json or "[]")
            completed = set(json.loads(log.completed_task_ids_json or "[]"))
            if not [t for t in planned if t not in completed]:
                return

        labels = {
            "morning": "Still haven't picked today's plan — even one small thing counts.",
            "midday": "Those tasks are still sitting there. Pick the smallest one.",
            "evening": "Still waiting on your wind-down check-in — takes 10 seconds.",
            "meal": f"Still haven't logged {ref} — even 'skip' is fine, just tap it.",
        }
        text = "⏰ " + labels.get(kind, "Still waiting on you for this one.")

        await context.bot.send_message(chat_id=int(user.telegram_chat_id), text=text)

        row.escalation_count += 1
        row.last_sent_at = datetime.utcnow()
        db.commit()

        next_step = step + 1
        if next_step < len(ESCALATION_MINUTES):
            context.job_queue.run_once(
                _job_escalate,
                when=timedelta(minutes=ESCALATION_MINUTES[next_step]),
                name=_name(user_id, "escalate", f"{kind}:{ref}"),
                data={"user_id": user_id, "kind": kind, "ref": ref, "day": day_iso, "step": next_step},
            )
    finally:
        db.close()


def mark_nudge_responded(db, user: User, day, kind: str, ref: str = "") -> None:
    row = (
        db.query(PendingNudge)
        .filter(PendingNudge.user_id == user.id, PendingNudge.day == day,
                PendingNudge.kind == kind, PendingNudge.ref == ref)
        .one_or_none()
    )
    if row and row.responded_at is None:
        row.responded_at = datetime.utcnow()
        db.commit()


# ── Daily jobs ────────────────────────────────────────────────────────────────

async def _job_morning(context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = context.job.data["user_id"]
    db = SessionLocal()
    try:
        user = db.get(User, user_id)
        if not user or user.state != "active":
            return
        day = today_in_tz(user.timezone)

        _, is_new = _get_or_make_nudge(db, user, day, "morning")
        if not is_new:
            return

        log = get_or_create_log(db, user, day)
        log.morning_prompted_at = datetime.utcnow()
        db.commit()

        try:
            tasks = list_active_tasks()
        except TodoistError as e:
            tasks = []
            todoist_note = f"\n\n(Todoist error: {e})"
        else:
            todoist_note = ""

        due_people = people_due_today(db, user, user.timezone)
        people_txt = ""
        if user.people_enabled and due_people:
            today = today_in_tz(user.timezone)
            lines = "\n".join(f"- {format_person_line(p, today)}" for p in due_people)
            people_txt = f"\n\nPeople due today:\n{lines}"

        streak_line = format_streak_line(db, user, day)

        # Going quiet for days is the actual failure mode this app exists to
        # catch, so a long absence changes the opening rather than cheerfully
        # carrying on as if nothing happened.
        gap = days_since_last_engagement(db, user, day)
        if gap is None or gap >= 3:
            away = "a while" if gap is None else f"{gap} days"
            opener = (
                f"Been {away} since you last checked in — no guilt, the tasks just "
                f"kept existing quietly.\n\nPick one small thing and we're back."
            )
        else:
            opener = f"Morning. {streak_line}"

        text = (
            f"{opener}\n\n"
            f"What's the plan for today? Tap 1-5 things below, then Confirm — "
            f"or Skip if today's not a planning day.{todoist_note}{people_txt}"
        )

        if not tasks:
            text += "\n\n(No active Todoist tasks found — add one by just texting me anything.)"

        # No server-side selection state: the message's own checkboxes are the
        # source of truth, so a redeploy mid-planning can't break the buttons.
        await context.bot.send_message(
            chat_id=int(user.telegram_chat_id), text=text,
            reply_markup=keyboards.morning_plan_keyboard(tasks, set()),
        )

        _schedule_escalation(context.application, user.id, "morning", "", day.isoformat())
    finally:
        db.close()


async def _job_midday(context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = context.job.data["user_id"]
    db = SessionLocal()
    try:
        user = db.get(User, user_id)
        if not user or user.state != "active":
            return
        day = today_in_tz(user.timezone)

        log = get_or_create_log(db, user, day)
        if log.midday_prompted_at is not None:
            return
        log.midday_prompted_at = datetime.utcnow()
        db.commit()

        planned_ids = json.loads(log.planned_task_ids_json or "[]")
        completed_ids = set(json.loads(log.completed_task_ids_json or "[]"))
        remaining_ids = [t for t in planned_ids if t not in completed_ids]

        if not remaining_ids:
            if planned_ids:
                text = "Midday check-in: today's picks are all done. 🎉 Anything else on your mind? Just text me."
            else:
                text = "Midday check-in: you haven't picked anything for today yet. Still exists — want to grab 1 thing now?"
            await context.bot.send_message(chat_id=int(user.telegram_chat_id), text=text)
            return

        try:
            all_tasks = {t.id: t for t in list_active_tasks()}
        except TodoistError:
            all_tasks = {}

        remaining_tasks = [all_tasks[i] for i in remaining_ids if i in all_tasks]
        lines = "\n".join(f"- {t.content}" for t in remaining_tasks) or "(details unavailable)"
        text = f"Midday check-in — these still exist:\n{lines}"

        await context.bot.send_message(
            chat_id=int(user.telegram_chat_id), text=text,
            reply_markup=keyboards.tasks_list_keyboard(remaining_tasks) if remaining_tasks else None,
        )

        # Midday is the resurfacing prompt -- the one that exists specifically to
        # stop tasks going out of sight. Firing it once and giving up defeats it,
        # so it escalates like the others when work is still outstanding.
        if remaining_tasks:
            _get_or_make_nudge(db, user, day, "midday")
            _schedule_escalation(context.application, user.id, "midday", "", day.isoformat())
    finally:
        db.close()


async def _job_evening(context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = context.job.data["user_id"]
    db = SessionLocal()
    try:
        user = db.get(User, user_id)
        if not user or user.state != "active":
            return
        day = today_in_tz(user.timezone)

        _, is_new = _get_or_make_nudge(db, user, day, "evening")
        if not is_new:
            return

        log = get_or_create_log(db, user, day)
        log.evening_prompted_at = datetime.utcnow()
        db.commit()

        planned_ids = json.loads(log.planned_task_ids_json or "[]")
        completed_ids = json.loads(log.completed_task_ids_json or "[]")
        streak_line = format_streak_line(db, user, day)

        text = (
            f"Wind-down. {len(completed_ids)}/{len(planned_ids)} planned things done today.\n"
            f"{streak_line}\n\n"
            f"Anything for tomorrow? Just text it to me — it'll be captured. "
            f"Or tap below and call it a day."
        )
        from telegram import InlineKeyboardMarkup, InlineKeyboardButton
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ That's today", callback_data="eveningdone")]])
        await context.bot.send_message(chat_id=int(user.telegram_chat_id), text=text, reply_markup=kb)

        _schedule_escalation(context.application, user.id, "evening", "", day.isoformat())
    finally:
        db.close()


async def _job_pocket_poll(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Pulls new Pocket action items on a timer. This is what makes the Pocket
    link work without a public webhook endpoint or a documented payload format."""
    from .services import pocket as pocket_svc
    from .services import pocket_sync
    from . import keyboards as kb

    if not pocket_svc.pocket_api_enabled():
        return

    db = SessionLocal()
    try:
        for user in db.query(User).filter(User.state == "active").all():
            try:
                new, _total = pocket_sync.pull_action_items(db, user)
            except pocket_svc.PocketError as e:
                print(f"Pocket poll failed: {e}")
                continue
            if not new:
                continue

            rows = (
                db.query(InboxSuggestion)
                .filter(InboxSuggestion.user_id == user.id,
                        InboxSuggestion.status == "pending")
                .order_by(InboxSuggestion.created_at.desc())
                .limit(new)
                .all()
            )
            if not rows:
                continue
            listing = "\n".join(f"• {r.text}" for r in rows)
            await context.bot.send_message(
                chat_id=int(user.telegram_chat_id),
                text=f"🎙️ Pocket picked up {len(rows)} action item(s):\n{listing}",
                reply_markup=kb.suggestions_keyboard(rows),
            )
    finally:
        db.close()


async def _job_meal(context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = context.job.data["user_id"]
    meal = context.job.data["meal"]
    db = SessionLocal()
    try:
        user = db.get(User, user_id)
        if not user or user.state != "active" or not user.meals_enabled:
            return
        day = today_in_tz(user.timezone)

        _, is_new = _get_or_make_nudge(db, user, day, "meal", meal)
        if not is_new:
            return

        text = f"{meal.capitalize()} check-in. Eat something? Send a pic if you want, or just tap below."
        await context.bot.send_message(
            chat_id=int(user.telegram_chat_id), text=text,
            reply_markup=keyboards.meal_keyboard(meal),
        )

        _schedule_escalation(context.application, user.id, "meal", meal, day.isoformat())
    finally:
        db.close()
