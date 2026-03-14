"""
Workout history analyser — pure logic, no AI.

Takes the recent activities list from garmin.fetch_daily_stats() and returns
a rolling-7-day summary dict that the workout recommender passes to Claude as context.

"This week" is defined as the rolling 7 days ending yesterday (not the ISO week),
so a Sunday hard session is never invisible on Monday.
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
        activities: List of activity dicts (from garmin.fetch_daily_stats()).
                    Each dict has: name, type, start_time, duration_seconds,
                    distance_meters, avg_hr, calories.

    Returns:
        {
            "total_sessions_this_week":  int,
            "km_run_this_week":          float,
            "run_sessions_this_week":    int,   # number of distinct run sessions
            "long_run_km_this_week":     float, # distance of the single longest run
            "gym_sessions_this_week":    int,
            "hours_since_last_workout":  float | None,
            "daily_activity_map":        dict[str, list[str]],  # "YYYY-MM-DD" → [label, ...]
            "consecutive_training_days": int,   # days in a row ending yesterday
            "trained_yesterday":         bool,
        }
    """
    today = date.today()
    week_start = today - timedelta(days=7)   # rolling 7-day window
    yesterday = today - timedelta(days=1)

    total_sessions = 0
    km_run = 0.0
    run_sessions = 0
    long_run_km = 0.0
    gym_sessions = 0
    daily_map: dict[str, list[str]] = {}   # date ISO → list of session labels
    last_workout_dt: datetime | None = None
    trained_yesterday = False

    for act in activities:
        dt = _parse_start(act.get("start_time"))
        if dt is None:
            continue

        if last_workout_dt is None or dt > last_workout_dt:
            last_workout_dt = dt

        act_date = dt.date()

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
            run_sessions += 1
            if dist_km > long_run_km:
                long_run_km = dist_km
            pace_str = _format_pace(act.get("avg_speed_mps"), act.get("distance_meters"), act.get("duration_seconds"))
            pace_part = f", avg pace {pace_str}" if pace_str else ""
            label = f"Run: {dist_km} km ({duration_min} min{pace_part})"
        elif act_type in _GYM_TYPES:
            gym_sessions += 1
            label = f"Gym — {name} ({duration_min} min)"
        else:
            label = f"{name} ({duration_min} min)"

        daily_map.setdefault(act_date.isoformat(), []).append(label)

    hours_since = None
    if last_workout_dt is not None:
        hours_since = round((datetime.now() - last_workout_dt).total_seconds() / 3600, 1)

    # Count consecutive training days ending on yesterday (today hasn't happened yet).
    # Walk back up to 7 days so we don't stop artificially at a Monday boundary.
    consecutive = 0
    check = yesterday
    while check >= week_start:
        if check.isoformat() in daily_map:
            consecutive += 1
            check -= timedelta(days=1)
        else:
            break

    return {
        "total_sessions_this_week": total_sessions,
        "km_run_this_week": round(km_run, 1),
        "run_sessions_this_week": run_sessions,
        "long_run_km_this_week": round(long_run_km, 1),
        "gym_sessions_this_week": gym_sessions,
        "hours_since_last_workout": hours_since,
        "daily_activity_map": daily_map,
        "consecutive_training_days": consecutive,
        "trained_yesterday": trained_yesterday,
    }
