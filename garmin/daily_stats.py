"""
Garmin Connect data-fetching module.

Auth model: credentials are stored per-user in DynamoDB as garth OAuth tokens
(JSON string). Call initial_login(user_id, email, password) once to authenticate
and persist the tokens. Subsequent calls use the stored tokens.

Public API:
  - initial_login(user_id, email, password)  — first-time auth, saves tokens
  - fetch_daily_stats(user_id, force_refresh) — today's health snapshot
  - fetch_week_stats(user_id, sunday, saturday) — aggregated Sun–Sat recovery summary
"""

import logging
import time
from datetime import date, datetime, timezone, timedelta

from garminconnect import Garmin, GarminConnectAuthenticationError

import storage

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def get_garmin_client(user_id: str) -> Garmin:
    """Return an authenticated Garmin client by restoring stored OAuth tokens.

    Args:
        user_id: Telegram user ID string.

    Raises:
        GarminConnectAuthenticationError: if no tokens are stored or they are invalid.
    """
    tokens_json = storage.load_garmin_tokens(user_id)
    if not tokens_json:
        raise GarminConnectAuthenticationError(
            f"No Garmin credentials for user {user_id} — run /connect_garmin"
        )
    client = Garmin()
    client.garth.loads(tokens_json)
    client.display_name = client.garth.profile.get("displayName")
    return client


def initial_login(user_id: str, email: str, password: str) -> None:
    """Authenticate with email + password, then persist the OAuth tokens.

    This is called once from the /connect_garmin wizard. After this, all
    subsequent calls use the saved tokens via get_garmin_client().

    Args:
        user_id:  Telegram user ID string.
        email:    Garmin Connect account email.
        password: Garmin Connect account password (not persisted).

    Raises:
        GarminConnectAuthenticationError: on bad credentials.
    """
    client = Garmin(email, password)
    client.login()
    storage.save_garmin_tokens(user_id, client.garth.dumps())
    logger.info("Garmin tokens saved for user %s.", user_id)


def _refresh_tokens(user_id: str, client: Garmin) -> None:
    """Silently persist tokens after a successful API call (garth may have refreshed them)."""
    try:
        storage.save_garmin_tokens(user_id, client.garth.dumps())
    except Exception as exc:
        logger.warning("Failed to refresh Garmin tokens for %s: %s", user_id, exc)


# ---------------------------------------------------------------------------
# Private helpers — each returns a dict and catches its own errors so that
# a single failing endpoint never breaks the whole briefing.
# ---------------------------------------------------------------------------

def _fetch_sleep(client: Garmin, day: str) -> dict:
    try:
        data = client.get_sleep_data(day)
        daily = data.get("dailySleepDTO", {})

        # sleepEndTimestampLocal is epoch-milliseconds; present only after watch syncs.
        wake_ts_ms = daily.get("sleepEndTimestampLocal")
        wake_time_iso = None
        if wake_ts_ms:
            wake_time_iso = datetime.fromtimestamp(
                wake_ts_ms / 1000, tz=timezone.utc
            ).isoformat()

        return {
            "sleep_score": daily.get("sleepScores", {}).get("overall", {}).get("value"),
            "total_sleep_seconds": daily.get("sleepTimeSeconds"),
            "deep_sleep_seconds": daily.get("deepSleepSeconds"),
            "light_sleep_seconds": daily.get("lightSleepSeconds"),
            "rem_sleep_seconds": daily.get("remSleepSeconds"),
            "awake_seconds": daily.get("awakeSleepSeconds"),
            # ISO-8601 UTC timestamp of when sleep ended (i.e. wake-up time).
            # None until the Garmin watch syncs after waking.
            "wake_time_utc": wake_time_iso,
        }
    except Exception as exc:
        return {"error": str(exc)}


def _fetch_hrv(client: Garmin, day: str) -> dict:
    try:
        data = client.get_hrv_data(day)
        summary = data.get("hrvSummary", {})
        return {
            "last_night_avg": summary.get("lastNight"),
            "last_night_5min_high": summary.get("lastNight5MinHigh"),
            "weekly_avg": summary.get("weeklyAvg"),
            "status": summary.get("hrv_status"),
        }
    except Exception as exc:
        return {"error": str(exc)}


_RECENT_STEPS_WINDOW_MINUTES = 60


def _fetch_steps(client: Garmin, day: str) -> dict:
    """Fetch step buckets for *day* and return total and recent step counts.

    Args:
        client: Authenticated Garmin client.
        day:    ISO date string (YYYY-MM-DD).

    Returns:
        {
            "total_steps":  int  — cumulative steps for the whole day,
            "recent_steps": int  — steps in buckets that started within the last
                                   _RECENT_STEPS_WINDOW_MINUTES minutes (UTC).
        }
    """
    try:
        buckets = client.get_steps_data(day)
        total = sum(entry.get("steps", 0) for entry in buckets)

        # Sum only buckets that started within the recent window.
        # Bucket timestamps are in UTC under the "startGMT" key,
        # format: "2026-03-09T22:00:00.0"
        cutoff_utc = datetime.now(timezone.utc) - timedelta(minutes=_RECENT_STEPS_WINDOW_MINUTES)
        recent_steps = 0
        for entry in buckets:
            ts_str = entry.get("startGMT")
            if not ts_str:
                continue
            try:
                bucket_start = datetime.strptime(ts_str, "%Y-%m-%dT%H:%M:%S.%f").replace(
                    tzinfo=timezone.utc
                )
                if bucket_start >= cutoff_utc:
                    recent_steps += entry.get("steps", 0)
            except ValueError:
                pass

        return {"total_steps": total, "recent_steps": recent_steps}
    except Exception as exc:
        return {"error": str(exc)}


def _fetch_last_activity(client: Garmin) -> dict:
    try:
        activities = client.get_activities(0, 1)
        if not activities:
            return {"error": "No activities found"}
        a = activities[0]
        return {
            "name": a.get("activityName"),
            "type": a.get("activityType", {}).get("typeKey"),
            "start_time": a.get("startTimeLocal"),
            "duration_seconds": a.get("duration"),
            "distance_meters": a.get("distance"),
            "avg_hr": a.get("averageHR"),
            "calories": a.get("calories"),
            "avg_speed_mps": a.get("averageSpeed"),  # metres per second; None for non-GPS
        }
    except Exception as exc:
        return {"error": str(exc)}


def _fetch_recent_activities(client: Garmin, limit: int = 14) -> list:
    try:
        activities = client.get_activities(0, limit)
        result = []
        for a in activities:
            result.append({
                "name": a.get("activityName"),
                "type": a.get("activityType", {}).get("typeKey"),
                "start_time": a.get("startTimeLocal"),
                "duration_seconds": a.get("duration"),
                "distance_meters": a.get("distance"),
                "avg_hr": a.get("averageHR"),
                "calories": a.get("calories"),
                "avg_speed_mps": a.get("averageSpeed"),
            })
        return result
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

# Per-user cache: user_id -> {"value": dict | None, "expires": float}
_cache: dict[str, dict] = {}
_CACHE_TTL = 7200  # seconds — refresh at most every two hours


def check_wake_status(user_id: str) -> dict:
    """Return only sleep and steps data required for wake detection to save API calls."""
    client = get_garmin_client(user_id)
    today = date.today().isoformat()
    result = {
        "sleep": _fetch_sleep(client, today),
        "steps": _fetch_steps(client, today),
    }
    _refresh_tokens(user_id, client)
    return result


def fetch_daily_stats(user_id: str, force_refresh: bool = False) -> dict | None:
    """Return Garmin daily stats for a user, using an in-memory cache by default.

    Args:
        user_id:       Telegram user ID string.
        force_refresh: When True, bypass the cache and always fetch from Garmin.
                       Exceptions are re-raised so the caller can report them.
                       When False (default), silently falls back to stale data on error.
    """
    user_cache = _cache.get(user_id, {"value": None, "expires": 0.0})
    if not force_refresh and time.monotonic() < user_cache["expires"] and user_cache["value"]:
        return user_cache["value"]
    try:
        data = _fetch_new_daily_stats(user_id)
        _cache[user_id] = {"value": data, "expires": time.monotonic() + _CACHE_TTL}
        return data
    except Exception:
        if force_refresh:
            raise
        return user_cache.get("value")  # return stale data if available


def _fetch_new_daily_stats(user_id: str) -> dict:
    """Authenticate and return today's health snapshot for the given user.

    Garmin files sleep/HRV under the date you *wake up*, so both are fetched
    for today (e.g. Monday morning → Monday's entry = Sunday night's sleep).

    Returns:
        {
            "date": "YYYY-MM-DD",
            "sleep": {...},
            "hrv":   {...},
            "steps": {...},
            "last_activity":      {...},
            "recent_activities":  [...],
        }

    Raises:
        GarminConnectAuthenticationError: on missing or invalid tokens.
    """
    client = get_garmin_client(user_id)
    today = date.today().isoformat()

    result = {
        "date": today,
        "sleep": _fetch_sleep(client, today),
        "hrv": _fetch_hrv(client, today),
        "steps": _fetch_steps(client, today),
        "last_activity": _fetch_last_activity(client),
        "recent_activities": _fetch_recent_activities(client),
    }

    # Persist tokens in case garth silently refreshed them during the requests.
    _refresh_tokens(user_id, client)
    return result


def _fetch_daily_sleep_summary(client: Garmin, day: str) -> dict:
    """Fetch just the sleep score and duration for a given date.

    Lighter wrapper around _fetch_sleep — returns only the two fields needed
    for weekly aggregation (score + total seconds).
    """
    try:
        data = client.get_sleep_data(day)
        daily = data.get("dailySleepDTO", {})
        return {
            "sleep_score": daily.get("sleepScores", {}).get("overall", {}).get("value"),
            "total_sleep_seconds": daily.get("sleepTimeSeconds"),
        }
    except Exception as exc:
        logger.debug("Sleep fetch failed for %s on %s: %s", client.display_name, day, exc)
        return {}


def _fetch_daily_steps_total(client: Garmin, day: str) -> int | None:
    """Fetch total step count for a given date via the user summary endpoint.

    Uses get_user_summary (one lightweight call) rather than the full step-bucket
    endpoint used for wake detection — we only need the daily total here.
    """
    try:
        summary = client.get_user_summary(day)
        return summary.get("totalSteps")
    except Exception as exc:
        logger.debug("Steps fetch failed for %s on %s: %s", day, client.display_name, exc)
        return None


def fetch_week_stats(user_id: str, sunday: date, saturday: date) -> dict:
    """Return an aggregated health summary for the Sun–Sat week.

    Makes ~15 API calls total:
      - 7 lightweight sleep calls (one per day)
      - 7 lightweight steps calls (one per day via user summary)
      - 1 HRV call on Saturday (weeklyAvg is already a Garmin-computed 7-day average)

    Each sub-call catches its own errors, so a single failing endpoint never
    breaks the whole summary. Returns {} if the user has no Garmin tokens.

    Args:
        user_id:  Telegram user ID string.
        sunday:   First day of the week (date object).
        saturday: Last day of the week (date object).

    Returns:
        {
            "avg_sleep_score":      int | None,
            "avg_sleep_duration_min": int | None,
            "hrv_trend":            str | None,   e.g. "stable around baseline"
            "total_steps":          int | None,
            "days_with_sleep_data": int,
        }
    """
    try:
        client = get_garmin_client(user_id)
    except Exception:
        return {}

    today = date.today()
    week_days = [
        (sunday + timedelta(days=i)).isoformat()
        for i in range(7)
        if sunday + timedelta(days=i) <= today
    ]

    # --- Sleep: 7 calls ---
    sleep_scores = []
    sleep_durations_sec = []
    for day in week_days:
        s = _fetch_daily_sleep_summary(client, day)
        if s.get("sleep_score") is not None:
            sleep_scores.append(s["sleep_score"])
        if s.get("total_sleep_seconds") is not None:
            sleep_durations_sec.append(s["total_sleep_seconds"])

    avg_sleep_score = round(sum(sleep_scores) / len(sleep_scores)) if sleep_scores else None
    avg_sleep_duration_min = (
        round(sum(sleep_durations_sec) / len(sleep_durations_sec) / 60)
        if sleep_durations_sec else None
    )

    # --- Steps: 7 calls ---
    total_steps = 0
    steps_found = False
    for day in week_days:
        steps = _fetch_daily_steps_total(client, day)
        if steps is not None:
            total_steps += steps
            steps_found = True

    # --- HRV: 1 call on the latest available day (Saturday when the week is complete,
    #          otherwise today — never request a future date). ---
    hrv_trend: str | None = None
    try:
        hrv_day = min(saturday, date.today())
        hrv_data = client.get_hrv_data(hrv_day.isoformat())
        summary = hrv_data.get("hrvSummary", {})
        last_night = summary.get("lastNight")
        weekly_avg = summary.get("weeklyAvg")
        if last_night is not None and weekly_avg is not None and weekly_avg > 0:
            ratio = last_night / weekly_avg
            if ratio >= 1.05:
                hrv_trend = "above baseline — good recovery"
            elif ratio <= 0.95:
                hrv_trend = "below baseline — take it easy"
            else:
                hrv_trend = "stable around baseline"
    except Exception as exc:
        logger.debug("HRV fetch failed for week ending %s: %s", saturday, exc)

    _refresh_tokens(user_id, client)

    return {
        "avg_sleep_score": avg_sleep_score,
        "avg_sleep_duration_min": avg_sleep_duration_min,
        "hrv_trend": hrv_trend,
        "total_steps": total_steps if steps_found else None,
        "days_with_sleep_data": len(sleep_scores),
    }
