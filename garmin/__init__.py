"""
garmin — Garmin Connect integration package.

Modules:
  daily_stats       — OAuth auth + today's health snapshot + weekly recovery summary
  activity_analyzer — Pure-logic weekly training summary from recent activities

All public symbols are re-exported here so callers can simply do:
    import garmin
    garmin.fetch_daily_stats(user_id)
    garmin.fetch_week_stats(user_id, sunday, saturday)
    garmin.initial_login(user_id, email, password)
    garmin.analyze_week(activities)
"""

from garmin.daily_stats import fetch_daily_stats, fetch_week_stats, initial_login, get_garmin_client
from garmin.activity_analyzer import analyze_week

__all__ = [
    "fetch_daily_stats",
    "fetch_week_stats",
    "initial_login",
    "get_garmin_client",
    "analyze_week",
]
