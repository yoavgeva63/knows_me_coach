"""
DynamoDB persistence layer for the fitness coach bot.
All database interaction is centralized here — no other module touches boto3.

Tables (names read from env vars, with sensible defaults):
  - fitness_coach_history  : per-user conversation message lists
  - fitness_coach_users    : per-user profile data + coach notes

Caching:
  - The boto3 resource is a module-level singleton (connection pool reuse).
  - Loaded profiles are cached in-memory and invalidated on every write,
    eliminating redundant DynamoDB GETs within the same process lifetime.
"""
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

HISTORY_TABLE = os.environ.get("DYNAMODB_HISTORY_TABLE", "fitness_coach_history")
PROFILE_TABLE = os.environ.get("DYNAMODB_USERS_TABLE", "fitness_coach_users")

MAX_MESSAGES = 30

_dynamodb = boto3.resource("dynamodb", region_name=os.environ.get("AWS_REGION", "eu-central-1"))

# In-memory profile cache — invalidated on every save_profile call.
_profile_cache: dict[str, dict] = {}


def ensure_tables() -> None:
    """Create DynamoDB tables if they don't exist. Safe to call on every startup."""
    for table_name in (HISTORY_TABLE, PROFILE_TABLE):
        try:
            _dynamodb.meta.client.describe_table(TableName=table_name)
            logger.info("DynamoDB table '%s' already exists.", table_name)
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ResourceNotFoundException":
                logger.info("Creating DynamoDB table '%s'...", table_name)
                table = _dynamodb.create_table(
                    TableName=table_name,
                    KeySchema=[{"AttributeName": "user_id", "KeyType": "HASH"}],
                    AttributeDefinitions=[{"AttributeName": "user_id", "AttributeType": "S"}],
                    BillingMode="PAY_PER_REQUEST",
                )
                table.wait_until_exists()
                logger.info("Table '%s' created.", table_name)
            else:
                raise


# ---------------------------------------------------------------------------
# Conversation history
# ---------------------------------------------------------------------------

def load_history(user_id: str) -> list[dict]:
    """Return the conversation message list for a user, or [] if none stored."""
    table = _dynamodb.Table(HISTORY_TABLE)
    try:
        resp = table.get_item(Key={"user_id": user_id})
        item = resp.get("Item")
        if item:
            return json.loads(item["messages"])
    except Exception as exc:
        logger.warning("load_history failed for %s: %s", user_id, exc)
    return []


def save_history(user_id: str, messages: list[dict]) -> None:
    """Persist the message list, trimmed to the last MAX_MESSAGES entries."""
    if len(messages) > MAX_MESSAGES:
        messages = messages[-MAX_MESSAGES:]
    table = _dynamodb.Table(HISTORY_TABLE)
    try:
        table.put_item(Item={
            "user_id": user_id,
            "messages": json.dumps(messages),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })
    except Exception as exc:
        logger.error("save_history failed for %s: %s", user_id, exc)


def clear_history(user_id: str) -> None:
    """Delete the stored conversation history for a user."""
    table = _dynamodb.Table(HISTORY_TABLE)
    try:
        table.delete_item(Key={"user_id": user_id})
    except Exception as exc:
        logger.error("clear_history failed for %s: %s", user_id, exc)


# ---------------------------------------------------------------------------
# User profile
# ---------------------------------------------------------------------------

def load_profile(user_id: str, fallback_path: str | None = None) -> dict:
    """Load the user profile, returning the cached copy if available.

    On first call per process, fetches from DynamoDB and caches the result.
    On first run ever (no record in DynamoDB), auto-seeds from fallback_path
    (user_profile.json) when provided, then persists it to DynamoDB.
    """
    if user_id in _profile_cache:
        return _profile_cache[user_id]

    table = _dynamodb.Table(PROFILE_TABLE)
    try:
        resp = table.get_item(Key={"user_id": user_id})
        item = resp.get("Item")
        if item:
            profile = json.loads(item["profile_data"])
            _profile_cache[user_id] = profile
            return profile
    except Exception as exc:
        logger.warning("load_profile failed for %s: %s", user_id, exc)

    # First run: seed from the local JSON file if available
    if fallback_path:
        path = Path(fallback_path)
        if path.exists():
            profile = json.loads(path.read_text(encoding="utf-8"))
            profile.setdefault("coach_notes", [])
            save_profile(user_id, profile)
            logger.info("Seeded DynamoDB profile for user %s from %s", user_id, fallback_path)
            return profile

    return {}


def save_profile(user_id: str, profile: dict) -> None:
    """Persist the full profile dict to DynamoDB and update the in-memory cache."""
    _profile_cache[user_id] = profile
    table = _dynamodb.Table(PROFILE_TABLE)
    try:
        table.put_item(Item={
            "user_id": user_id,
            "profile_data": json.dumps(profile),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })
    except Exception as exc:
        logger.error("save_profile failed for %s: %s", user_id, exc)


def add_coach_note(user_id: str, note: str) -> None:
    """Append a timestamped note to the user's coach_notes list."""
    profile = load_profile(user_id)
    if not profile:
        logger.warning("add_coach_note: no profile found for user %s", user_id)
        return
    notes = profile.setdefault("coach_notes", [])
    notes.append({
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "note": note,
    })
    save_profile(user_id, profile)


# ---------------------------------------------------------------------------
# Morning alarm preferences
# ---------------------------------------------------------------------------

def get_morning_prefs(user_id: str) -> dict:
    """Return the user's morning alarm preferences.

    Returns a dict with:
      - "alarm_time": "HH:MM" (Israel time) or "sleep" for Garmin-based trigger.
                      Defaults to "09:00" if never set.
      - "sent_date":  "YYYY-MM-DD" of the last day a morning briefing was sent,
                      or None if never sent.
    """
    profile = load_profile(user_id)
    return {
        "alarm_time": profile.get("morning_alarm_time", "09:00"),
        "sent_date": profile.get("morning_sent_date"),
    }


def set_morning_alarm(user_id: str, alarm_time: str) -> None:
    """Persist the user's preferred morning alarm time.

    Args:
        alarm_time: "HH:MM" in Israel time (e.g. "07:30") or "sleep" to
                    trigger automatically when Garmin detects wake-up.
    """
    profile = load_profile(user_id)
    profile["morning_alarm_time"] = alarm_time
    save_profile(user_id, profile)


def mark_morning_sent(user_id: str, date_str: str) -> None:
    """Record that the morning briefing was already sent on date_str (YYYY-MM-DD)."""
    profile = load_profile(user_id)
    profile["morning_sent_date"] = date_str
    save_profile(user_id, profile)


# ---------------------------------------------------------------------------
# Daily workout cache
# ---------------------------------------------------------------------------

def save_daily_workout(user_id: str, workout: dict, date_str: str) -> None:
    """Cache today's pre-generated workout in the user profile.

    Args:
        workout:  Dict with keys: summary, motivation, full_recommendation, recovery_tier.
        date_str: Today's date as YYYY-MM-DD — used to detect staleness tomorrow.
    """
    profile = load_profile(user_id)
    profile["daily_workout"] = {"date": date_str, **workout}
    save_profile(user_id, profile)


def load_daily_workout(user_id: str, date_str: str) -> dict | None:
    """Return today's cached workout, or None if not generated yet or from a previous day.

    Args:
        date_str: Today's date as YYYY-MM-DD.
    """
    profile = load_profile(user_id)
    cached = profile.get("daily_workout")
    if cached and cached.get("date") == date_str:
        return cached
    return None