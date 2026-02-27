"""
Garmin Connect data-fetching module.
Public API: fetch_daily_stats(force_refresh=False)
"""

import os
import time
from datetime import date

from dotenv import load_dotenv
from garminconnect import Garmin, GarminConnectAuthenticationError

load_dotenv()


def get_garmin_client() -> Garmin:
    """Authenticate and return a Garmin client.

    Raises:
        ValueError: if credentials are missing from the environment.
        GarminConnectAuthenticationError: if login fails.
    """
    email = os.getenv("GARMIN_EMAIL")
    password = os.getenv("GARMIN_PASSWORD")
    if not email or not password:
        raise ValueError("GARMIN_EMAIL and GARMIN_PASSWORD must be set in .env")
    client = Garmin(email, password)
    client.login()
    return client


# ---------------------------------------------------------------------------
# Private helpers — each returns a dict and catches its own errors so that
# a single failing endpoint never breaks the whole briefing.
# ---------------------------------------------------------------------------

def _fetch_sleep(client: Garmin, day: str) -> dict:
    try:
        data = client.get_sleep_data(day)
        daily = data.get("dailySleepDTO", {})
        return {
            "sleep_score": daily.get("sleepScores", {}).get("overall", {}).get("value"),
            "total_sleep_seconds": daily.get("sleepTimeSeconds"),
            "deep_sleep_seconds": daily.get("deepSleepSeconds"),
            "light_sleep_seconds": daily.get("lightSleepSeconds"),
            "rem_sleep_seconds": daily.get("remSleepSeconds"),
            "awake_seconds": daily.get("awakeSleepSeconds"),
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


def _fetch_steps(client: Garmin, day: str) -> dict:
    try:
        buckets = client.get_steps_data(day)
        total = sum(entry.get("steps", 0) for entry in buckets)
        return {"total_steps": total}
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
        }
    except Exception as exc:
        return {"error": str(exc)}


def _fetch_recent_activities(client: Garmin, limit: int = 7) -> list:
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
            })
        return result
    except Exception as exc:
        return []


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

_cache: dict = {"value": None, "expires": 0.0}
_CACHE_TTL = 7200  # seconds — refresh at most every two hours


def fetch_daily_stats(force_refresh: bool = False) -> dict | None:
    """Return Garmin daily stats, using an in-memory cache by default.

    Args:
        force_refresh: When True, bypass the cache and always fetch from Garmin.
                       Exceptions are re-raised so the caller can report them.
                       When False (default), silently falls back to stale data on error.
    """
    if not force_refresh and time.monotonic() < _cache["expires"] and _cache["value"]:
        return _cache["value"]
    try:
        data = _fetch_new_daily_stats()
        _cache["value"] = data
        _cache["expires"] = time.monotonic() + _CACHE_TTL
        return data
    except Exception:
        if force_refresh:
            raise
        return _cache["value"]  # return stale data if available


def _fetch_new_daily_stats() -> dict:
    """Authenticate and return today's health snapshot.

    Garmin files sleep/HRV under the date you *wake up*, so both are fetched
    for today (e.g. Monday morning → Monday's entry = Sunday night's sleep).
    Steps are also fetched for today.

    Returns:
        {
            "date": "YYYY-MM-DD",
            "sleep": {...},
            "hrv":   {...},
            "steps": {...},
            "last_activity": {...},
        }

    Raises:
        GarminConnectAuthenticationError: on bad credentials.
        ValueError: if credentials are missing.
    """
    client = get_garmin_client()
    today = date.today().isoformat()

    return {
        "date": today,
        "sleep": _fetch_sleep(client, today),
        "hrv": _fetch_hrv(client, today),
        "steps": _fetch_steps(client, today),
        "last_activity": _fetch_last_activity(client),
        "recent_activities": _fetch_recent_activities(client),
    }
