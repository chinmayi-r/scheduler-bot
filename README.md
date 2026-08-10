# scheduler-bot

A Telegram bot for managing ADHD/procrastination-friendly daily structure: tasks,
calendar visibility, meals, and people you don't want to ghost.

## Design philosophy

This was rebuilt from scratch around a few specific problems: tasks you don't
write down because writing them down takes effort, tasks that stop existing the
moment they're out of sight, and an all-or-nothing streak that makes one bad day
feel like total failure. Concretely:

- **Zero-syntax capture.** Any message you send — text or voice note — becomes a
  real task instantly. No command syntax to remember.
- **Todoist is the source of truth for tasks**, not a private database. The bot
  only orchestrates nudges and capture against it — you can always open Todoist
  directly, edit there, see it on a widget, whatever you want.
- **Visibility, repeatedly.** A morning ping asks you to pick a small handful of
  things for today; a midday ping resurfaces whatever's still open, so tasks
  don't just disappear once you stop looking at the chat.
- **"Never miss twice."** A single missed day doesn't reset your streak — two
  in a row does. Flexible tracking like this holds up longer than rigid
  daily-perfection streaks.
- **Escalating nudges.** If you ignore a prompt, it re-pings at increasing
  intervals instead of firing once and giving up.
- **Grounded people-tracking.** Contacts hold running notes on who they are and
  what you last talked about — not just a countdown timer.
- **Calendar is read-only context**, not another thing to maintain.

## Using it

**Type anything and it becomes a task.** That's the primary interface. Voice
notes work the same way (transcribed, then captured), which is usually faster
than typing mid-thought.

A **persistent button row** sits above the keyboard permanently — 📋 Today,
✅ Tasks, 🔨 Break down, ⚙️ More — so the app stays visible instead of only
existing when you remember it. All commands also appear under Telegram's Menu
button, so nothing has to be memorised.

- `/today` — calendar, today's plan, people due, meals, streak
- `/tasks` — active tasks; tap ✅ to finish, 🔨 to break one down
- `/breakdown` — split a task that feels too big into steps, first one under 2 min
- `/people` — people you're tracking; ✅ contacted, 💤 snooze, 📝 add a note
- `/meals` — today's meal check-ins
- `/streak` — how you're doing
- `/settings` — timezone, ping times, feature toggles
- `/test morning|midday|evening|meal` — fire a prompt now to check it works
- `/cancel` — escape any half-finished flow
- `/help` — short, with buttons to drill in

Send a photo and it attaches to the open meal check-in (or the next unlogged
meal). Abandoned flows expire on their own, and whatever you type next is
captured as a task rather than swallowed.

## Optional integrations

**Break it down** (`LLM_API_KEY`) turns a vague task into 3-5 concrete steps
whose first step takes under two minutes — the point is defeating task-initiation
paralysis, not planning. Works against any OpenAI-compatible endpoint and
defaults to OpenRouter's free tier; it's one call per button press.


## Setup

1. Create a bot with [@BotFather](https://t.me/BotFather) and get `TELEGRAM_BOT_TOKEN`.
2. Get a Todoist API token: Todoist Settings -> Integrations -> Developer.
3. Copy `.env.example` to `.env` and fill in the required values.
4. `pip install -r requirements.txt`
5. `python -m app.bot`
6. Message your bot `/start` and follow the setup conversation (timezone, ping
   times, meals, people, optional calendar link).

## Deploying to Railway

1. Push this repo to GitHub, then create a new Railway project from it.
2. Set the env vars from `.env.example` in the Railway service settings.
3. Add a volume mounted somewhere like `/data`, and set
   `DATABASE_URL=sqlite:////data/schedulerbot.db` so task/streak/people data
   survives redeploys. (A Postgres URL works too, if you'd rather use Railway's
   Postgres addon.)
4. Railway builds via Nixpacks and runs the `worker` process from `Procfile`
   automatically — no separate deploy workflow needed once the repo is linked.

## Architecture

- `app/bot.py` — Telegram handlers: onboarding, buttons, capture, voice/photo
- `app/scheduler.py` — per-user, timezone-aware recurring jobs (morning/midday/
  evening/meals) plus the escalation ladder for ignored prompts
- `app/migrate.py` — non-destructive startup migration from the pre-redesign schema
- `app/db.py` — SQLAlchemy models: `User`, `DailyLog` (streak/plan tracking),
  `PendingNudge` (dedupe + escalation), `Person`, `MealLog`
- `app/services/` — Todoist, Google Calendar (ICS), people, meals, streaks,
  voice transcription, capture, LLM breakdown

Two deliberate design choices worth knowing if you edit this:

- **Morning-plan selection lives in the message, not the server.** The
  checkboxes in the inline keyboard *are* the state, so a redeploy mid-planning
  can't invalidate the buttons.
- **Multi-step flows expire.** An abandoned flow releases after 15 minutes and
  the next thing you type is captured normally instead of being consumed as an
  answer.
