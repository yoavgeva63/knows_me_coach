"""
Workout recommender — orchestrates the rules layer and calls Claude.

Flow:
  1. classify_recovery()   — pure rules, converts Garmin data → recovery tier
  2. analyze_week()        — pure rules, converts activities  → weekly summary
  3. Build a structured prompt with interpreted context only (no raw HRV ms)
  4. Claude decides the specific workout and writes it in full detail

Claude never sees raw HRV numbers — only the tier, its meaning, and constraints.
"""

from datetime import date

from brain import get_workout_briefing
from recovery import classify_recovery
from garmin_activity_analyzer import analyze_week


_TIER_CONTEXT = {
    "high": (
        "Recovery is HIGH. HRV is at or above the 7-day baseline and sleep was "
        "strong. The body is primed to adapt to hard training."
    ),
    "moderate": (
        "Recovery is MODERATE. One or both of HRV and sleep are slightly below "
        "optimal but the body is still ready to train. Avoid going to absolute "
        "failure — keep a rep or two in the tank."
    ),
    "low": (
        "Recovery is LOW. Stress markers are elevated and/or sleep was poor. "
        "Training is still appropriate but intensity must stay conservative. "
        "The goal is stimulus without digging a deeper hole."
    ),
    "very_low": (
        "Recovery is VERY LOW. The body is clearly under-recovered. "
        "A hard session today would be counterproductive. "
        "Recommend active recovery only — light movement, mobility, walking."
    ),
}


def _event_blurb(target_event: dict) -> str:
    """Return a one-line event context string, or empty string if no event set."""
    name = (target_event or {}).get("name", "").strip()
    date_str = (target_event or {}).get("date", "").strip()
    if not name or not date_str:
        return ""
    try:
        event_date = date.fromisoformat(date_str)
        days_left = (event_date - date.today()).days
    except ValueError:
        return f"Target event: {name}"

    if days_left <= 0:
        return f"{name} has passed (or is today)."
    elif days_left <= 7:
        return (
            f"RACE WEEK — {name} is in {days_left} day(s). "
            "No hard efforts. Prioritise rest, stay loose, protect the legs."
        )
    elif days_left <= 14:
        return (
            f"TAPER — {name} is in {days_left} days. "
            "Reduce volume, keep a touch of sharpness, avoid injury risk."
        )
    elif days_left <= 28:
        return (
            f"Final build — {name} is in {days_left} days. "
            "Balance quality sessions with adequate recovery."
        )
    else:
        return f"Target event: {name} in {days_left} days."



def get_workout_recommendation(
    garmin_data: dict,
    user_profile: dict,
    weather: str = "",
    conversation_history: list[dict] | None = None,
) -> dict:
    """
    Generate a personalised morning workout recommendation.

    Args:
        garmin_data:          Output of garmin.fetch_daily_stats().
        user_profile:         User profile dict including coach_notes.
        weather:              One-line weather string (optional).
        conversation_history: Prior conversation messages — gives Claude context
                              about things discussed before /morning was called.

    Returns:
        {
            "recommendation": str,   # Claude's full workout text
            "recovery_tier":  str,   # "high" | "moderate" | "low" | "very_low"
        }
    """
    # ── 1. Classify recovery (rules only) ────────────────────────────────────
    sleep = garmin_data.get("sleep", {})
    hrv = garmin_data.get("hrv", {})

    recovery = classify_recovery(
        sleep_score=sleep.get("sleep_score"),
        hrv_last_night=hrv.get("last_night_avg"),
        hrv_weekly_avg=hrv.get("weekly_avg"),
    )

    # ── 2. Analyse workout history (rules only) ───────────────────────────────
    history = analyze_week(garmin_data.get("recent_activities", []))

    # ── 3. Assemble prompt context ────────────────────────────────────────────
    name = user_profile.get("name", "Athlete")
    age = user_profile.get("age", "N/A")
    weight = user_profile.get("weight_kg", "N/A")
    level = user_profile.get("fitness_level", "intermediate")
    primary_goal = (
        user_profile.get("primary_goal")
        or user_profile.get("fitness_goal", "general fitness")
    )
    secondary_goal = user_profile.get("secondary_goal", "")
    weekly_days = (
        user_profile.get("weekly_training_days")
        or user_profile.get("workouts_per_week", 5)
    )
    session_duration = user_profile.get("preferred_session_duration_minutes", 60)
    event_blurb = _event_blurb(user_profile.get("target_event", {}))
    coach_notes = user_profile.get("coach_notes", [])

    tier = recovery["tier"]

    # Week summary lines
    week_lines = history["last_week_activities"]
    week_summary = (
        "\n".join(f"  - {line}" for line in week_lines)
        if week_lines
        else "  - No sessions recorded this week yet"
    )
    hours_since = history["hours_since_last_workout"]
    hours_str = f"{hours_since:.0f} hours ago" if hours_since is not None else "unknown"

    # ── 4. Build the prompt ───────────────────────────────────────────────────
    coach_notes_block = ""
    if coach_notes:
        notes_lines = "\n".join(f"  - {n.get('date', '?')}: {n.get('note', '')}" for n in coach_notes)
        coach_notes_block = f"\n## Coach Notes (long-term memory — respect these)\n{notes_lines}"

    prompt = f"""You are a personal fitness coach writing a morning workout recommendation.

## Athlete
- Name: {name}, Age: {age}, Weight: {weight} kg, Level: {level}
- Primary goal: {primary_goal}
{f"- Secondary goal: {secondary_goal}" if secondary_goal else ""}\
{f"- {event_blurb}" if event_blurb else ""}
- Weekly training days: {weekly_days} | Preferred session: {session_duration} min{coach_notes_block}

## Today's Recovery — {recovery["label"]}
{_TIER_CONTEXT[tier]}
Coaching note: {recovery["note"]}
Intensity ceiling: {recovery["intensity_ceiling"]} (max RPE {recovery["max_rpe"]}/10)

## This Week's Training So Far
{week_summary}
- Sessions this week: {history["total_sessions_this_week"]}
- Km run this week: {history["km_run_this_week"]} km
- Gym sessions this week: {history["gym_sessions_this_week"]}
- Last workout: {hours_str}
- Trained yesterday: {"Yes" if history["trained_yesterday"] else "No"}

## Weather
{weather if weather else "N/A"}

## Your Task
Write today's morning briefing for {name}:
- summary: one sentence — workout type and RPE (e.g. "Push day — RPE 7, heavy compound work (~60 min)")
- motivation: one sentence drawing on the athlete's goal or recent context — no fluff
- workout_recommendation: start directly with the workout title and structure (no greeting, no recovery recap). Then the full detailed session plan. Close with a single coaching note referencing today's recovery tier (e.g. "Recovery is solid today — feel free to push." or "Recovery is moderate — keep 1-2 reps in the tank on every set."). Be specific:
  - Gym session → exact exercises, sets × reps per exercise
  - Run → distance, structure (warmup / main set / cooldown), pace guidance via speed or RPE
  - Active recovery → exactly what to do and for how long
  Respect the intensity ceiling above. Target ~{session_duration} min total. Concise and actionable — under 230 words.

Do not reference or react to any prior conversation in summary or motivation. Use prior context only in workout_recommendation if directly relevant (e.g. user mentioned soreness)."""

    # ── 5. Call Claude via brain.py (structured output) ──────────────────────
    result = get_workout_briefing(prompt, conversation_history)

    return {**result, "recovery_tier": tier}
