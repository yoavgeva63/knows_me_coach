# CLAUDE.md — AI Instructions for the Fitness Coach Bot

This file is automatically read by Claude Code at the start of every session.
Follow every rule here strictly and consistently.

---

## Project Overview

A personal fitness coaching Telegram bot powered by Claude AI (Anthropic).
It integrates with Garmin Connect for health data, persists state in AWS DynamoDB,
and runs as a systemd service on Oracle Cloud Free Tier (Ubuntu 22.04).

The bot is a **single-user personal tool** — not a multi-tenant SaaS.

---

## Architecture & File Map

| File | Responsibility |
|---|---|
| `bot.py` | Telegram handlers (entry point for local dev, long-polling) |
| `brain.py` | All Claude AI calls — conversation, system prompt, context injection |
| `storage.py` | **Only** module that touches DynamoDB / boto3 |
| `briefing.py` | Morning briefing logic, weather fetch, Markdown → HTML |
| `garmin_daily_stats.py` | Fetch today's Garmin stats (sleep, HRV, steps, last workout) |
| `garmin_activity_analyzer.py` | Deeper activity analysis from Garmin |
| `recovery.py` | Recovery scoring logic |
| `workout_recommender.py` | Workout suggestion logic |
| `user_profile.json` | Seed profile (first-run seed for DynamoDB) |
| `proccess_explanation.md` | Developer ops guide (SSH, deploy, systemd commands) |
| `requirements.txt` | Python dependencies |
| `.env` | Local secrets (never commit) |

---

## Tech Stack

- **Language**: Python 3.11+
- **AI**: Anthropic SDK (`anthropic`) — model `claude-sonnet-4-6`
- **Bot framework**: `python-telegram-bot` v21
- **Database**: AWS DynamoDB via `boto3`
- **Garmin**: `garminconnect` + `garth`
- **Config**: `python-dotenv` (.env for local, env vars in production)
- **Deployment**: Oracle Cloud VM, systemd service named `knows_me_coach`

---

## Coding Conventions

### Every module MUST have a module-level docstring
Explain what the module does and any key design decisions.
```python
"""
DynamoDB persistence layer for the fitness coach bot.
All database interaction is centralized here — no other module touches boto3.
"""
```

### Every function MUST have a docstring
Include what it does, args (when non-obvious), and return value.
```python
def load_history(user_id: str) -> list[dict]:
    """Return the conversation message list for a user, or [] if none stored."""
```

### Private helpers are prefixed with `_`
Helper functions not meant to be called from other modules use `_name`.
```python
def _fmt_seconds(s) -> str: ...
def _format_garmin_context(garmin_data: dict) -> str: ...
```

### Use type hints on all function signatures
```python
def get_claude_response(
    conversation_history: list[dict],
    user_message: str,
    weather: str = "",
    garmin_data: dict | None = None,
) -> str:
```

### Logging — use the module-level logger
```python
logger = logging.getLogger(__name__)
logger.info("...")
logger.warning("...")
logger.error("...")
```
Never use `print()` for operational output.

### Environment variables — never hardcode secrets
Read from `os.environ` with sensible defaults where safe:
```python
os.environ["ANTHROPIC_API_KEY"]          # required, raise if missing
os.environ.get("AWS_REGION", "us-east-1")  # optional with default
```

---

## Architecture Rules (enforce these every time)

1. **Single responsibility per module** — `storage.py` is the ONLY place boto3/DynamoDB is touched.
   No other module should import boto3 or query DynamoDB directly.

2. **No code duplication** — Before writing a new helper, check if an existing function already does it.
   If similar logic exists in two places, extract it into a shared helper.

3. **All Claude calls go through `brain.py`** — No other module should call `anthropic.Anthropic()`.

4. **All Garmin auth/session logic stays in `garmin_daily_stats.py`** — other modules call its public functions.

5. **Error handling at boundaries** — Catch exceptions at the Telegram handler level (`bot.py`) and
   at external API calls (Garmin, weather). Internal functions may let exceptions bubble up.

6. **Never commit secrets** — `.env`, `user_profile.json` (contains personal data), and `ssh_keys/`
   must not be committed. Check `.gitignore` before adding files.

---

## Code Style Rules

- **Concise over verbose** — Don't add unnecessary complexity, extra abstractions, or future-proofing.
- **No feature flags or backwards-compat shims** — just change the code.
- **No redundant comments** — comment only when the logic isn't self-evident from the code.
- **Imports** — standard library first, then third-party, then local modules (separated by blank lines).
- **String formatting** — prefer f-strings.
- **Max line length** — 100 characters (soft limit, use judgment).

---

## Deployment Workflow (for reference, don't run unless asked)

```bash
# Connect to server
cd "C:\Users\gevay\Desktop\programing\my_coach_agent\ssh_keys"
ssh -i private-ssh-key.key ubuntu@129.159.141.62

# Deploy update
cd knows_me_coach && git pull
sudo systemctl restart knows_me_coach

# Check status / logs
sudo systemctl status knows_me_coach
sudo journalctl -u knows_me_coach -f
```

Local dev:
```bash
venv\Scripts\activate   # Windows
source venv/bin/activate  # Linux (server)
python bot.py
```

---

## When Adding New Features

1. Read the relevant existing module(s) before writing any code.
2. Follow the module's existing patterns exactly (docstrings, logging, error handling style).
3. If the feature needs a new external service/API, create a dedicated module for it (like `garmin_daily_stats.py`).
4. If the feature needs persistence, add functions to `storage.py` only.
5. If the feature needs Claude, add a new function to `brain.py` only.
6. Update `requirements.txt` if new packages are needed.
7. Update `proccess_explanation.md` if new operational steps are required.
8. Update `CLAUDE.md` respectively