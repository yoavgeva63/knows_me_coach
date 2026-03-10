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
from datetime import datetime, timedelta, timezone

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

def load_profile(user_id: str) -> dict:
    """Load the user profile, returning the cached copy if available.

    On first call per process, fetches from DynamoDB and caches the result.
    Returns an empty dict if no profile exists yet (user must run /start).
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


MAX_COACH_NOTES = 20


def add_coach_note(user_id: str, note: str) -> None:
    """Append a timestamped note to the user's coach_notes list, capped at MAX_COACH_NOTES.

    When the cap is reached, the oldest notes are dropped first.
    """
    profile = load_profile(user_id)
    if not profile:
        logger.warning("add_coach_note: no profile found for user %s", user_id)
        return
    notes = profile.setdefault("coach_notes", [])
    notes.append({
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "note": note,
    })
    if len(notes) > MAX_COACH_NOTES:
        dropped = len(notes) - MAX_COACH_NOTES
        profile["coach_notes"] = notes[dropped:]
        logger.info("Pruned %d oldest coach note(s) for user %s", dropped, user_id)
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

def patch_daily_workout(user_id: str, fields: dict, date_str: str) -> None:
    """Update specific fields of today's cached workout.

    Caller is responsible for ensuring a base workout exists for date_str before calling
    (e.g. by calling save_daily_workout first if load_daily_workout returns None).

    Args:
        fields: Fields to update — supports 'workout_recommendation' and 'summary'.
        date_str: Today's date as YYYY-MM-DD.
    """
    profile = load_profile(user_id)
    existing = profile.get("daily_workout", {})
    if existing.get("date") != date_str:
        logger.warning("patch_daily_workout: no workout cached for %s on %s — skipping", user_id, date_str)
        return
    workout = dict(existing)
    workout.update({k: v for k, v in fields.items() if v is not None})
    profile["daily_workout"] = workout
    save_profile(user_id, profile)


def save_daily_workout(user_id: str, workout: dict, date_str: str) -> None:
    """Cache today's pre-generated workout in the user profile.

    Args:
        workout:  Dict with keys: summary, motivation, workout_recommendation, recovery_tier.
        date_str: Today's date as YYYY-MM-DD — used to detect staleness tomorrow.
    """
    profile = load_profile(user_id)
    profile["daily_workout"] = {"date": date_str, **workout}
    save_profile(user_id, profile)


# ---------------------------------------------------------------------------
# Garmin tokens
# ---------------------------------------------------------------------------

def save_garmin_tokens(user_id: str, tokens_json: str) -> None:
    """Persist Garmin OAuth tokens (garth JSON string) inside the user profile."""
    profile = load_profile(user_id)
    profile["garmin_tokens"] = tokens_json
    save_profile(user_id, profile)


def load_garmin_tokens(user_id: str) -> str | None:
    """Return stored Garmin OAuth tokens string, or None if not connected yet."""
    profile = load_profile(user_id)
    return profile.get("garmin_tokens")


# ---------------------------------------------------------------------------
# Nutrition tracking
# ---------------------------------------------------------------------------

_NUTRITION_HISTORY_DAYS = 7


def _get_nutrition(profile: dict) -> dict:
    """Return the nutrition sub-dict from a profile, creating it if absent."""
    return profile.setdefault("nutrition", {})


def load_daily_meals(user_id: str, date_str: str) -> list[dict]:
    """Return the list of logged meals for date_str, or [] if none.

    Each meal dict contains: name, slot, kcal, protein_g, fat_g, carbs_g, logged_at.
    """
    profile = load_profile(user_id)
    return _get_nutrition(profile).get("daily_meals", {}).get(date_str, [])


def get_meals_from_profile(profile: dict, date_str: str) -> list[dict]:
    """Extract today's logged meals from an already-loaded profile dict.

    Avoids a redundant DynamoDB fetch when the caller has already called
    load_user_data() and has the profile in memory.

    Args:
        profile:  Profile dict returned by load_user_data() or load_profile().
        date_str: Today's date as YYYY-MM-DD.

    Returns:
        List of meal dicts (may be empty). Each dict has: name, slot, kcal,
        protein_g, fat_g, carbs_g, logged_at.
    """
    return _get_nutrition(profile).get("daily_meals", {}).get(date_str, [])


def save_daily_meal(user_id: str, date_str: str, meal: dict) -> None:
    """Append a logged meal to today's list and prune entries older than 7 days.

    Args:
        date_str: Date key as YYYY-MM-DD.
        meal:     Dict with keys: name, slot, kcal, protein_g, fat_g, carbs_g, logged_at.
    """
    profile = load_profile(user_id)
    nutrition = _get_nutrition(profile)
    daily = nutrition.setdefault("daily_meals", {})
    daily.setdefault(date_str, []).append(meal)

    # Prune dates older than _NUTRITION_HISTORY_DAYS to prevent profile bloat.
    cutoff = (
        datetime.now(timezone.utc).date()
        - timedelta(days=_NUTRITION_HISTORY_DAYS)
    ).isoformat()
    nutrition["daily_meals"] = {d: v for d, v in daily.items() if d >= cutoff}

    save_profile(user_id, profile)


def replace_daily_meal(user_id: str, date_str: str, slot: str, meal: dict) -> None:
    """Replace the logged meal for a given slot, or append if none exists yet.

    Args:
        slot: "breakfast", "lunch", or "dinner".
        meal: Full meal dict to store.
    """
    profile = load_profile(user_id)
    nutrition = _get_nutrition(profile)
    meals = nutrition.setdefault("daily_meals", {}).setdefault(date_str, [])
    nutrition["daily_meals"][date_str] = [m for m in meals if m.get("slot") != slot]
    nutrition["daily_meals"][date_str].append(meal)
    save_profile(user_id, profile)


def log_meal(user_id: str, date_str: str, meal: dict) -> None:
    """Log a meal and clear pending options in a single DynamoDB write.

    Replaces the existing meal for the same slot if one was already logged today,
    appends otherwise. Also prunes history and clears pending_options atomically,
    avoiding the two-write pattern of save_daily_meal + clear_pending_meal_options.

    Args:
        date_str: Date key as YYYY-MM-DD.
        meal:     Dict with keys: name, slot, kcal, protein_g, fat_g, carbs_g, logged_at.
    """
    profile = load_profile(user_id)
    nutrition = _get_nutrition(profile)
    slot = meal["slot"]
    daily = nutrition.setdefault("daily_meals", {})
    existing = daily.setdefault(date_str, [])
    daily[date_str] = [m for m in existing if m.get("slot") != slot] + [meal]

    cutoff = (
        datetime.now(timezone.utc).date()
        - timedelta(days=_NUTRITION_HISTORY_DAYS)
    ).isoformat()
    nutrition["daily_meals"] = {d: v for d, v in daily.items() if d >= cutoff}
    nutrition.pop("pending_options", None)

    save_profile(user_id, profile)


def save_groceries(user_id: str, ingredient_str: str) -> None:
    """Persist the user's current ingredient list with a timestamp."""
    profile = load_profile(user_id)
    nutrition = _get_nutrition(profile)
    nutrition["last_groceries"] = ingredient_str
    nutrition["last_groceries_updated_at"] = datetime.now(timezone.utc).isoformat()
    save_profile(user_id, profile)


def load_groceries(user_id: str) -> tuple[str | None, str | None]:
    """Return (ingredient_str, updated_at_iso) or (None, None) if never set."""
    profile = load_profile(user_id)
    nutrition = _get_nutrition(profile)
    return nutrition.get("last_groceries"), nutrition.get("last_groceries_updated_at")


def save_pending_meal_options(user_id: str, slot: str, options: list[dict]) -> None:
    """Cache the two meal options Claude just generated, keyed to today + slot.

    Stored so the [✅ Option 1] / [✅ Option 2] buttons can retrieve full meal
    data without encoding it in callback_data (Telegram limit: 64 bytes).

    Args:
        slot:    "breakfast", "lunch", or "dinner".
        options: List of meal dicts (name, kcal, protein_g, fat_g, carbs_g, time_min, reasoning).
    """
    profile = load_profile(user_id)
    nutrition = _get_nutrition(profile)
    nutrition["pending_options"] = {
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "slot": slot,
        "options": options,
    }
    save_profile(user_id, profile)


def load_pending_meal_options(user_id: str) -> dict | None:
    """Return the cached pending meal options dict, or None if not set."""
    profile = load_profile(user_id)
    return _get_nutrition(profile).get("pending_options")


def clear_pending_meal_options(user_id: str) -> None:
    """Remove pending meal options after the user has logged or dismissed them."""
    profile = load_profile(user_id)
    _get_nutrition(profile).pop("pending_options", None)
    save_profile(user_id, profile)


# ---------------------------------------------------------------------------
# User enumeration
# ---------------------------------------------------------------------------

def list_all_user_ids() -> list[str]:
    """Return every user_id present in the profile table.

    Uses a ProjectionExpression scan so only the key attribute is transferred.
    Suitable for small user bases (personal tool, not paginated).
    """
    table = _dynamodb.Table(PROFILE_TABLE)
    try:
        resp = table.scan(ProjectionExpression="user_id")
        return [item["user_id"] for item in resp.get("Items", [])]
    except Exception as exc:
        logger.error("list_all_user_ids failed: %s", exc)
        return []


def load_user_data(user_id: str, date_str: str) -> tuple[dict, dict | None]:
    """Single DynamoDB GET returning both the user profile and today's cached workout.

    Replaces the pattern of calling load_profile() + load_daily_workout() separately,
    which would hit the same table item twice. Always bypasses the in-memory cache
    (same rationale as load_daily_workout — a cron process may have written the workout).

    Args:
        date_str: Today's date as YYYY-MM-DD, used to validate workout freshness.

    Returns:
        (profile, daily_workout) where daily_workout is None if not generated yet
        or was generated on a different date.
    """
    table = _dynamodb.Table(PROFILE_TABLE)
    try:
        resp = table.get_item(Key={"user_id": user_id})
        item = resp.get("Item")
        if item:
            profile = json.loads(item["profile_data"])
            _profile_cache[user_id] = profile
            cached = profile.get("daily_workout")
            workout = cached if (cached and cached.get("date") == date_str) else None
            return profile, workout
    except Exception as exc:
        logger.warning("load_user_data failed for %s: %s", user_id, exc)
    return {}, None


def load_daily_workout(user_id: str, date_str: str) -> dict | None:
    """Return today's cached workout, or None if not generated yet or from a previous day.

    Always reads directly from DynamoDB (bypassing the in-memory profile cache) because
    the workout may have been written by a separate process (e.g. morning_check.py cron).

    Args:
        date_str: Today's date as YYYY-MM-DD.
    """
    table = _dynamodb.Table(PROFILE_TABLE)
    try:
        resp = table.get_item(Key={"user_id": user_id})
        item = resp.get("Item")
        if item:
            profile = json.loads(item["profile_data"])
            # Also refresh the in-memory cache so subsequent calls are consistent.
            _profile_cache[user_id] = profile
            cached = profile.get("daily_workout")
            if cached and cached.get("date") == date_str:
                return cached
    except Exception as exc:
        logger.warning("load_daily_workout failed for %s: %s", user_id, exc)
    return None