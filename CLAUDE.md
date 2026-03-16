# CLAUDE.md — AI Instructions for the Fitness Coach Bot

This file is automatically read by Claude Code at the start of every session.
Follow every rule here strictly and consistently.

---

## Project Overview

A personal fitness coaching Telegram bot powered by Claude AI (Anthropic).
Integrates with Garmin Connect for health data, persists state in AWS DynamoDB,
and runs as a systemd service on Oracle Cloud Free Tier (Ubuntu 22.04).
---

## Architecture & File Map

| File | Responsibility |
|---|---|
| `bot.py` | Telegram entry point — command handlers, briefing button callbacks, tool execution |
| `utils/time_utils.py` | Israel-time helpers — `israel_now()`, `israel_today()`, `TZ_ISRAEL`; single source of truth for all date computation |
| `utils/__init__.py` | Re-exports `israel_now`, `israel_today`, `TZ_ISRAEL` — import via `from utils import ...` |
| `brain/conversation.py` | Main coach chat — `SYSTEM_PROMPT`, `ACTION_TOOLS`, `get_claude_response` |
| `brain/workout.py` | Structured morning briefing — `get_workout_briefing` |
| `brain/nutrition.py` | Meal suggestions — `get_meal_suggestions`, `get_ingredient_meal` |
| `brain/memory.py` | Long-term fact extraction — `extract_memorable_facts` |
| `brain/workout_log.py` | Workout completion interpreter — `interpret_workout_modification` (ConversationHandler path only) |
| `brain/weekly_summary.py` | Weekly Coach's Take — `get_weekly_coaches_take` |
| `brain/__init__.py` | Re-exports all public brain functions; **only module that imports `anthropic`** |
| `storage.py` | **Only** module that touches DynamoDB / boto3 |
| `briefing.py` | Morning briefing — build message, inline keyboard, cache workout, send; gracefully skips recovery line for non-Garmin users |
| `weekly_briefing.py` | Weekly summary — build Sun–Sat review message, send; auto-triggered Saturdays 19:00 and via `/weekly` |
| `profile_wizard.py` | `/start` + `/profile` ConversationHandler wizard — collects `weekly_gym_days`, `weekly_run_days`, expanded goal picker (body comp / running distance / custom) |
| `nutrition.py` | Macro formula (Mifflin-St Jeor), daily totals, message formatters, Claude prompt builders |
| `nutrition_handlers.py` | Telegram routing — ingredient ConversationHandler + `nutr:` callback dispatcher |
| `workout_recommender.py` | Sport-aware workout prompt builder (gym / run / combined mode derived from `weekly_gym_days` + `weekly_run_days`); calls `brain.get_workout_briefing` |
| `garmin/daily_stats.py` | Fetch today's Garmin stats (`fetch_daily_stats`) and aggregated Sun–Sat recovery summary (`fetch_week_stats`) |
| `garmin/activity_analyzer.py` | Rolling 7-day activity analysis — tracks `run_sessions_this_week`, `long_run_km_this_week`, gym sessions |
| `garmin/__init__.py` | Re-exports `fetch_daily_stats`, `fetch_week_stats`, `initial_login`, `analyze_week` for clean imports |
| `recovery.py` | Pure-rules recovery tier classification |
| `proccess_explanation.md` | Developer ops guide (SSH, deploy, systemd) |
| `requirements.txt` | Python dependencies |
| `.env` | Local secrets — never commit |

**Detailed feature docs → `docs/` folder** (read the relevant file before touching a feature):
- `docs/briefing_system.md` — morning briefing flow, inline buttons, workout caching
- `docs/profile_wizard.md` — wizard states, field map, skip-filled logic
- `docs/nutrition.md` — nutrition flow, macro formula, DynamoDB schema, callback routing, ingredient ConversationHandler
- `docs/weekly_summary.md` — weekly review flow, workout completion logging, data sources, message format

---

## Architecture Rules (enforce every time)

1. **All Claude calls go through the `brain/` package** — no other module imports `anthropic`.
2. **All DynamoDB access goes through `storage.py`** — no other module imports `boto3`.
3. **All Garmin auth/session logic stays in `garmin/daily_stats.py`**.
4. **Single responsibility** — before adding logic to a module, check it belongs there.
5. **No duplication** — before writing a helper, check if one already exists.
6. **Error handling at boundaries** — catch at Telegram handlers and external API calls; let internal functions bubble up.
7. **Never commit secrets** — `.env`, `user_profile.json`, `ssh_keys/` must stay out of git.

---

## Date / Time Convention

**All user-visible dates use Israel time (UTC+2), not server UTC.**

The server runs on UTC. Israel is UTC+2 (UTC+3 during DST — we approximate as +2).
Between midnight UTC and 02:00 AM Israel time, `date.today()` and
`datetime.now(timezone.utc).strftime("%Y-%m-%d")` return yesterday's date — causing
workouts, meals, history entries, and storage keys to land on the wrong day.

**Rule:** Any date used as a storage key, displayed to the user, or compared against
stored dates **must** be computed via `utils`:

```python
from utils import israel_now, israel_today, TZ_ISRAEL

today_str = israel_today()                          # → "2026-03-14"
now = israel_now()                                  # → timezone-aware datetime in Israel time
yesterday = (israel_now() - timedelta(days=1)).strftime("%Y-%m-%d")
local_dt = some_utc_datetime.astimezone(TZ_ISRAEL)  # convert a known UTC timestamp
```

Never use `date.today()`, `datetime.now()`, or `datetime.now(timezone.utc).strftime(...)` for user dates.
If DST-awareness is ever needed, update `utils/time_utils.py` — no other files change.

**Exceptions (UTC is correct):**
- Garmin API calls — Garmin's own date fields are UTC-based.
- Labels explicitly shown as UTC (e.g. `"fetched at 08:30 UTC"`).
- `coach_notes[].date` metadata — this is for human reference only, not a key.

---

## Coding Conventions

- Module-level docstring on every file explaining purpose and key design decisions.
- Docstring on every function (what it does, non-obvious args, return value).
- Private helpers prefixed with `_`.
- Type hints on all function signatures.
- `logger = logging.getLogger(__name__)` — never use `print()`.
- Standard library → third-party → local imports, separated by blank lines.
- f-strings for string formatting. 100-char soft line limit.

---

## Deployment (don't run unless asked)

```bash
cd "C:\Users\gevay\Desktop\programing\my_coach_agent\ssh_keys"
ssh -i private-ssh-key.key ubuntu@129.159.141.62
cd knows_me_coach && git pull && sudo systemctl restart knows_me_coach
sudo journalctl -u knows_me_coach -f
```

Local dev: `venv\Scripts\activate` then `python bot.py`

---

## Natural Language Action Tools

`brain.py` exposes `ACTION_TOOLS` — Claude may call these as side effects within a single LLM response (no second call needed for state-change actions):

| Tool | Effect |
|---|---|
| `set_morning_alarm(time)` | Calls `storage.set_morning_alarm()` |
| `remember_fact(fact)` | Calls `storage.add_coach_note()` |
| `trigger_morning_briefing()` | Calls `briefing.send_morning_briefing()` |
| `trigger_weekly_briefing()` | Calls `weekly_briefing.send_weekly_briefing()` |
| `update_daily_workout(workout_recommendation, summary?)` | Calls `storage.patch_daily_workout()`, generating a base via `get_workout_recommendation` first if none exists today |
| `log_workout_status(status, actual_summary?, actual_type?)` | Calls `storage.update_workout_status()` — logs done/modified/skipped from natural language |

**Adding a new tool:** define it in `ACTION_TOOLS` in `brain.py`, add the execution branch in `bot.py`'s `handle_message` tool loop.

**Destructive / complex actions** (clear history, profile update, Garmin reconnect) are **not tools** — Claude is instructed in `SYSTEM_PROMPT` to direct the user to the relevant slash command instead (`/clear`, `/profile`, `/connect_garmin`).

**Cached workout key:** `workout_recommendation` (renamed from `full_recommendation` — all pipeline stages use this name consistently).

**workout_history:** Rolling log of `{date, summary, status?, actual_summary?, actual_type?}` dicts stored in the user profile (max 14 entries = 7 days × 2 sessions). Written by `briefing.py` after every morning briefing via `storage.append_workout_history()`. Status fields (`done`/`modified`/`skipped`) are added later via `storage.update_workout_status()` — triggered by the Done/Modify/Skip buttons or the `log_workout_status` tool. Used by `workout_recommender.py` and `weekly_briefing.py`.

---

## When Adding or Changing Features

1. Read the relevant module(s) and `docs/` file before writing any code.
2. Follow the module's existing patterns exactly.
3. New external service → new dedicated module or package (like `garmin/`).
4. New persistence → add to `storage.py` only.
5. New Claude call → add to the relevant `brain/` submodule only.
6. New natural language action → add tool to `ACTION_TOOLS` in `brain/conversation.py` + handler in `bot.py`.
7. Update `requirements.txt` if new packages are needed.
8. **Update `CLAUDE.md` file map and the relevant `docs/` file when done.**
   If no `docs/` file exists for the feature yet, create one under `docs/<feature>.md`.
