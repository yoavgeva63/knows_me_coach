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
| `brain.py` | **All** Claude API calls — conversation (with action tools), workout briefing, fact extraction |
| `storage.py` | **Only** module that touches DynamoDB / boto3 |
| `briefing.py` | Morning briefing — build message, inline keyboard, cache workout, send |
| `profile_wizard.py` | `/start` + `/profile` ConversationHandler wizard (8-field setup) |
| `workout_recommender.py` | Build workout prompt from Garmin + profile, call `brain.get_workout_briefing` |
| `garmin_daily_stats.py` | Fetch today's Garmin stats (sleep, HRV, steps, activities) |
| `garmin_activity_analyzer.py` | Weekly activity analysis from Garmin data |
| `recovery.py` | Pure-rules recovery tier classification |
| `proccess_explanation.md` | Developer ops guide (SSH, deploy, systemd) |
| `requirements.txt` | Python dependencies |
| `.env` | Local secrets — never commit |

**Detailed feature docs → `docs/` folder** (read the relevant file before touching a feature):
- `docs/briefing_system.md` — morning briefing flow, inline buttons, workout caching
- `docs/profile_wizard.md` — wizard states, field map, skip-filled logic

---

## Architecture Rules (enforce every time)

1. **All Claude calls go through `brain.py`** — no other module imports `anthropic`.
2. **All DynamoDB access goes through `storage.py`** — no other module imports `boto3`.
3. **All Garmin auth/session logic stays in `garmin_daily_stats.py`**.
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

---

## When Adding or Changing Features

1. Read the relevant module(s) and `docs/` file before writing any code.
2. Follow the module's existing patterns exactly.
3. New external service → new dedicated module (like `garmin_daily_stats.py`).
4. New persistence → add to `storage.py` only.
5. New Claude call → add to `brain.py` only.
6. New natural language action → add tool to `ACTION_TOOLS` in `brain.py` + handler in `bot.py`.
7. Update `requirements.txt` if new packages are needed.
8. **Update `CLAUDE.md` file map and the relevant `docs/` file when done.**
   If no `docs/` file exists for the feature yet, create one under `docs/<feature>.md`.
