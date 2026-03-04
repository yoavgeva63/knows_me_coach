"""
Workout history analyser — pure logic, no AI.

Takes the recent activities list from garmin.fetch_daily_stats() and returns
a weekly summary dict that the workout recommender passes to Claude as context.

"This week" is defined as Monday through today (ISO week).
"""

from datetime import datetime, date, timedelta


# Activity type keys returned by Garmin Connect
_RUN_TYPES = {
    "running", "trail_running", "treadmill_running", "track_running",
    "indoor_running", "virtual_run",
}

_GYM_TYPES = {
    "strength_training", "fitness_equipment", "weight_training",
    "functional_strength_training", "hiit", "indoor_cycling",
    "elliptical", "yoga", "pilates",
}


def _parse_start(start_time_str: str | None) -> datetime | None:
    """Parse Garmin's startTimeLocal into a datetime. Returns None on failure."""
    if not start_time_str:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(start_time_str, fmt)
        except ValueError:
            continue
    return None


def _format_pace(avg_speed_mps: float | None, distance_meters: float | None, duration_seconds: float | None) -> str | None:
    """Return average pace as 'M:SS/km', or None if insufficient data.

    Prefers averageSpeed from Garmin; falls back to distance/duration ratio.
    """
    speed = avg_speed_mps
    if not speed and distance_meters and duration_seconds and duration_seconds > 0:
        speed = distance_meters / duration_seconds
    if not speed or speed <= 0:
        return None
    pace_sec_per_km = 1000 / speed
    mins = int(pace_sec_per_km // 60)
    secs = int(pace_sec_per_km % 60)
    return f"{mins}:{secs:02d}/km"


def analyze_week(activities: list) -> dict:
    """
    Summarise the current week's training from a list of recent Garmin activities.

    Args:
        activities: List of activity dicts (from garmin._fetch_recent_activities).
                    Each dict has: name, type, start_time, duration_seconds,
                    distance_meters, avg_hr, calories.

    Returns:
        {
            "total_sessions_this_week": int,
            "km_run_this_week":         float,
            "gym_sessions_this_week":   int,
            "hours_since_last_workout": float | None,
            "last_week_activities":     list[str],   # human-readable per session
            "trained_yesterday":        bool,
        }
    """
    today = date.today()
    week_start = today - timedelta(days=today.weekday())   # Monday of this week
    yesterday = today - timedelta(days=1)

    total_sessions = 0
    km_run = 0.0
    gym_sessions = 0
    week_labels: list[str] = []
    last_workout_dt: datetime | None = None
    trained_yesterday = False

    for act in activities:
        dt = _parse_start(act.get("start_time"))
        if dt is None:
            continue

        # Track the most recent workout across all fetched activities
        if last_workout_dt is None or dt > last_workout_dt:
            last_workout_dt = dt

        act_date = dt.date()

        # Only count sessions that fall in the current ISO week
        if act_date < week_start:
            continue

        total_sessions += 1

        if act_date == yesterday:
            trained_yesterday = True

        act_type = (act.get("type") or "").lower()
        duration_min = round((act.get("duration_seconds") or 0) / 60)
        dist_km = round((act.get("distance_meters") or 0) / 1000, 1)
        name = act.get("name") or act_type or "Workout"

        if act_type in _RUN_TYPES or "run" in act_type:
            km_run += dist_km
            pace_str = _format_pace(act.get("avg_speed_mps"), act.get("distance_meters"), act.get("duration_seconds"))
            pace_part = f", avg pace {pace_str}" if pace_str else ""
            week_labels.append(f"Run: {dist_km} km ({duration_min} min{pace_part})")
        elif act_type in _GYM_TYPES:
            gym_sessions += 1
            week_labels.append(f"Gym — {name} ({duration_min} min)")
        else:
            week_labels.append(f"{name} ({duration_min} min)")

    hours_since = None
    if last_workout_dt is not None:
        hours_since = round((datetime.now() - last_workout_dt).total_seconds() / 3600, 1)

    return {
        "total_sessions_this_week": total_sessions,
        "km_run_this_week": round(km_run, 1),
        "gym_sessions_this_week": gym_sessions,
        "hours_since_last_workout": hours_since,
        "last_week_activities": week_labels,
        "trained_yesterday": trained_yesterday,
    }
