"""
Garmin Connect data-fetching module.
Provides a single public function, fetch_daily_stats(), that returns the
health snapshot used by the morning briefing and other bot features.
"""

import os
from datetime import date, timedelta

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

def fetch_daily_stats() -> dict:
    """Authenticate and return today's health snapshot.

    Sleep and HRV are fetched for *yesterday* (Garmin always lags by one day).
    Steps are fetched for today.

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
    yesterday = (date.today() - timedelta(days=1)).isoformat()

    return {
        "date": today,
        "sleep": _fetch_sleep(client, yesterday),
        "hrv": _fetch_hrv(client, yesterday),
        "steps": _fetch_steps(client, today),
        "last_activity": _fetch_last_activity(client),
        "recent_activities": _fetch_recent_activities(client),
    }
