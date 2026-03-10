"""
Private context-formatting helpers shared across the brain package.

These functions convert raw data (Garmin stats, user profile, conversation history,
nutrition) into plain-text strings ready to be injected into Claude system/user prompts.
None of these functions make API calls.
"""

from nutrition import calculate_macros, compute_remaining, compute_totals


def _fmt_seconds(s) -> str:
    """Convert a duration in seconds to a human-readable 'Xh Ym' string."""
    if not isinstance(s, (int, float)):
        return "N/A"
    h, m = divmod(int(s) // 60, 60)
    return f"{h}h {m}m"


def _fmt_last_workout(activity: dict) -> str:
    """Format a single Garmin activity dict into a one-line summary string."""
    base = (
        f"Last workout: {activity.get('name', 'N/A')} ({activity.get('type', 'N/A')}) "
        f"on {activity.get('start_time', 'N/A')}, "
        f"duration {_fmt_seconds(activity.get('duration_seconds'))}, "
        f"avg HR {activity.get('avg_hr', 'N/A')} bpm"
    )
    dist_m = activity.get("distance_meters")
    speed_mps = activity.get("avg_speed_mps")
    if dist_m:
        base += f", distance {round(dist_m / 1000, 2)} km"
    if speed_mps:
        pace_sec_per_km = 1000 / speed_mps
        pace_min = int(pace_sec_per_km // 60)
        pace_sec = int(pace_sec_per_km % 60)
        base += f", pace {pace_min}:{pace_sec:02d} min/km"
    return base


def format_garmin_context(garmin_data: dict) -> str:
    """Convert a Garmin daily stats dict into a multi-line context string for Claude.

    Args:
        garmin_data: Dict returned by garmin.fetch_daily_stats().

    Returns:
        Multi-line string summarising sleep, HRV, steps, and recent activities.
    """
    sleep = garmin_data.get("sleep", {})
    hrv = garmin_data.get("hrv", {})
    steps = garmin_data.get("steps", {})
    activity = garmin_data.get("last_activity", {})
    recent = garmin_data.get("recent_activities", [])

    lines = [
        f"Date: {garmin_data.get('date', 'N/A')}",
        f"Sleep score: {sleep.get('sleep_score', 'N/A')}/100, "
        f"total: {_fmt_seconds(sleep.get('total_sleep_seconds'))}, "
        f"deep: {_fmt_seconds(sleep.get('deep_sleep_seconds'))}, "
        f"REM: {_fmt_seconds(sleep.get('rem_sleep_seconds'))}",
        f"HRV: {hrv.get('last_night_avg', 'N/A')} ms (weekly avg {hrv.get('weekly_avg', 'N/A')} ms, "
        f"status: {hrv.get('status', 'N/A')})",
        f"Steps today: {steps.get('total_steps', 'N/A')}",
        _fmt_last_workout(activity),
    ]

    if recent:
        recent_summary = "; ".join(
            f"{a.get('name', 'N/A')} {_fmt_seconds(a.get('duration_seconds'))} on {a.get('start_time', 'N/A')}"
            for a in recent[:5]
        )
        lines.append(f"Recent activities (last 5): {recent_summary}")

    return "\n".join(lines)


def format_profile_context(user_profile: dict) -> str:
    """Format user profile + coach notes for injection into the Claude system prompt.

    Args:
        user_profile: Profile dict from storage.load_profile().

    Returns:
        Multi-line string with profile fields and coach notes sections.
    """
    lines = [
        "## User Profile",
        f"Name: {user_profile.get('name', 'N/A')} | "
        f"Age: {user_profile.get('age', 'N/A')} | "
        f"Weight: {user_profile.get('weight_kg', 'N/A')} kg | "
        f"Height: {user_profile.get('height_cm', 'N/A')} cm | "
        f"Level: {user_profile.get('fitness_level', 'N/A')}",
        f"Primary goal: {user_profile.get('primary_goal', user_profile.get('fitness_goal', 'N/A'))}",
        f"Secondary goal: {user_profile.get('secondary_goal', 'N/A')}",
        f"Training days/week: {user_profile.get('weekly_training_days', user_profile.get('workouts_per_week', 'N/A'))} | "
        f"Session duration: {user_profile.get('preferred_session_duration_minutes', 'N/A')} min",
    ]

    target = user_profile.get("target_event", {})
    if target and target.get("name"):
        lines.append(f"Target event: {target['name']} on {target.get('date', 'TBD')}")

    notes = user_profile.get("coach_notes", [])
    if notes:
        lines.append("\n## Coach Notes (long-term memory)")
        for n in notes:
            lines.append(f"- {n.get('date', '?')}: {n.get('note', '')}")

    return "\n".join(lines)


def format_nutrition_context(profile: dict, logged_meals: list[dict]) -> str:
    """Format today's nutrition status for injection into the Claude system prompt.

    Args:
        profile:      User profile dict (used to derive daily macro targets).
        logged_meals: Meals logged today from storage.get_meals_from_profile().

    Returns:
        Multi-line string covering daily targets, meals eaten, and remaining macros.
    """
    targets = calculate_macros(profile)
    lines = [
        "## Today's Nutrition",
        f"Daily targets: {targets['kcal']} kcal | {targets['protein_g']}g protein | "
        f"{targets['fat_g']}g fat | {targets['carbs_g']}g carbs",
    ]

    if logged_meals:
        for m in logged_meals:
            lines.append(
                f"- {m['slot'].capitalize()}: {m['name']} — "
                f"{m['kcal']} kcal | {m['protein_g']}g protein | "
                f"{m['fat_g']}g fat | {m['carbs_g']}g carbs"
            )
        totals = compute_totals(logged_meals)
        remaining = compute_remaining(targets, logged_meals)
        lines.append(
            f"Eaten so far: {totals['kcal']} kcal | {totals['protein_g']}g protein | "
            f"{totals['fat_g']}g fat | {totals['carbs_g']}g carbs"
        )
        lines.append(
            f"Remaining: {remaining['kcal']} kcal | {remaining['protein_g']}g protein | "
            f"{remaining['fat_g']}g fat | {remaining['carbs_g']}g carbs"
        )
    else:
        lines.append("No meals logged yet today.")

    return "\n".join(lines)


def clean_history(conversation_history: list[dict]) -> list[dict]:
    """Prepare conversation history for the Anthropic API (role + content only).

    User messages are prefixed with their date so Claude can judge temporal relevance.
    Args:
        conversation_history: Raw history list from storage.load_history().

    Returns:
        Cleaned list of {"role": str, "content": str} dicts.
    """
    result = []
    for m in conversation_history:
        ts = m.get("ts")
        if m["role"] == "user" and ts:
            content = f"[{ts}] {m['content']}"
        else:
            content = m["content"]
        result.append({"role": m["role"], "content": content})
    return result
