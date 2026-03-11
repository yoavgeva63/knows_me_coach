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
| `brain/conversation.py` | Main coach chat — `SYSTEM_PROMPT`, `ACTION_TOOLS`, `get_claude_response` |
| `brain/workout.py` | Structured morning briefing — `get_workout_briefing` |
| `brain/nutrition.py` | Meal suggestions — `get_meal_suggestions`, `get_ingredient_meal` |
| `brain/memory.py` | Long-term fact extraction — `extract_memorable_facts` |
| `brain/__init__.py` | Re-exports all public brain functions; **only module that imports `anthropic`** |
| `storage.py` | **Only** module that touches DynamoDB / boto3 |
| `briefing.py` | Morning briefing — build message, inline keyboard, cache workout, send; gracefully skips recovery line for non-Garmin users |
| `profile_wizard.py` | `/start` + `/profile` ConversationHandler wizard (9-field setup, includes height) |
| `nutrition.py` | Macro formula (Mifflin-St Jeor), daily totals, message formatters, Claude prompt builders |
| `nutrition_handlers.py` | Telegram routing — ingredient ConversationHandler + `nutr:` callback dispatcher |
| `workout_recommender.py` | Build workout prompt from Garmin + profile (or workout_history for non-Garmin), call `brain.get_workout_briefing` |
| `garmin/daily_stats.py` | Fetch today's Garmin stats (sleep, HRV, steps, activities) |
| `garmin/activity_analyzer.py` | Rolling 7-day activity analysis from Garmin data |
| `garmin/__init__.py` | Re-exports `fetch_daily_stats`, `initial_login`, `analyze_week` for clean imports |
| `recovery.py` | Pure-rules recovery tier classification |
| `proccess_explanation.md` | Developer ops guide (SSH, deploy, systemd) |
| `requirements.txt` | Python dependencies |
| `.env` | Local secrets — never commit |

**Detailed feature docs → `docs/` folder** (read the relevant file before touching a feature):
- `docs/briefing_system.md` — morning briefing flow, inline buttons, workout caching
- `docs/profile_wizard.md` — wizard states, field map, skip-filled logic
- `docs/nutrition.md` — nutrition flow, macro formula, DynamoDB schema, callback routing, ingredient ConversationHandler

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
| `update_daily_workout(workout_recommendation, summary?)` | Calls `storage.patch_daily_workout()`, generating a base via `get_workout_recommendation` first if none exists today |

**Adding a new tool:** define it in `ACTION_TOOLS` in `brain.py`, add the execution branch in `bot.py`'s `handle_message` tool loop.

**Destructive / complex actions** (clear history, profile update, Garmin reconnect) are **not tools** — Claude is instructed in `SYSTEM_PROMPT` to direct the user to the relevant slash command instead (`/clear`, `/profile`, `/connect_garmin`).

**Cached workout key:** `workout_recommendation` (renamed from `full_recommendation` — all pipeline stages use this name consistently).

**workout_history:** Rolling log of `{date, summary}` dicts stored in the user profile (max 14 entries = 7 days × 2 sessions). Written by `briefing.py` after every morning briefing via `storage.append_workout_history()`. Used by `workout_recommender.py` as the primary training context for non-Garmin users and as supplemental context for Garmin users.

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
