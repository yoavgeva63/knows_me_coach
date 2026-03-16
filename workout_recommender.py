"""
Workout recommender — orchestrates the rules layer and calls Claude.

Flow:
  1. classify_recovery()   — pure rules, converts Garmin data → recovery tier
  2. analyze_week()        — pure rules, converts activities  → weekly summary
  3. Build a structured prompt with interpreted context only (no raw HRV ms)
  4. Claude decides the specific workout and writes it in full detail

Sport mode is derived from the user profile:
  - gym-only:  weekly_gym_days > 0 and weekly_run_days == 0
  - run-only:  weekly_run_days > 0 and weekly_gym_days == 0
  - combined:  both > 0 (conflict-aware scheduling)

Claude never sees raw HRV numbers — only the tier, its meaning, and constraints.
"""

from datetime import date, timedelta

from brain import get_workout_briefing
from utils import israel_now
from recovery import classify_recovery
from garmin import analyze_week


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


def _sport_mode(user_profile: dict) -> str:
    """Derive sport mode from profile training day split.

    Returns 'gym', 'run', or 'combined'.
    Falls back to 'gym' for legacy profiles that only have weekly_training_days.
    """
    gym = user_profile.get("weekly_gym_days", 0) or 0
    run = user_profile.get("weekly_run_days", 0) or 0
    if gym > 0 and run > 0:
        return "combined"
    if run > 0:
        return "run"
    return "gym"


def _running_target_blurb(user_profile: dict) -> str:
    """Return a coaching note about the user's running target distance, if set.

    Only uses the explicit running_target_km field (set by the wizard's distance sub-state).
    """
    km = user_profile.get("running_target_km")
    if not km:
        return ""
    km_val = float(km)
    if km_val <= 5:
        return "Race target ≤ 5 km — speed and VO2max are the priority. Include weekly intervals."
    if km_val <= 10:
        return "Race target 5–10 km — mix of speed work and tempo. One quality session per week."
    if km_val <= 21:
        return (
            "Race target half marathon (≤ 21 km) — aerobic base + threshold tempo. "
            "Weekly long run is essential; keep easy runs truly easy."
        )
    if km_val <= 42:
        return (
            "Race target marathon (≤ 42 km) — high aerobic volume, long runs up to ~32 km. "
            "80% of runs should be easy/conversational pace."
        )
    return (
        f"Race target ultra ({km_val} km) — volume and time-on-feet dominate. "
        "Easy effort almost always; protect joints."
    )


def _event_blurb(target_event: dict) -> str:
    """Return a one-line event context string, or empty string if no event set."""
    name = (target_event or {}).get("name", "").strip()
    date_str = (target_event or {}).get("date", "").strip()
    if not name or not date_str:
        return ""
    try:
        event_date = date.fromisoformat(date_str)
        days_left = (event_date - israel_now().date()).days
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



def _build_week_schedule(daily_map: dict[str, list[str]]) -> str:
    """Format the day-indexed activity map into a readable schedule block.

    Shows the rolling 7 days ending yesterday. Today is omitted (that's what we're deciding).
    """
    today = israel_now().date()
    lines = []
    for offset in range(7, 0, -1):   # 7 days ago → yesterday
        day = today - timedelta(days=offset)
        day_name = f"{day.strftime('%a')} {day.day} {day.strftime('%b')}"
        sessions = daily_map.get(day.isoformat(), [])
        if sessions:
            lines.append(f"  {day_name}: " + " | ".join(sessions))
        else:
            lines.append(f"  {day_name}: Rest")
    return "\n".join(lines) if lines else "  No days logged yet"


def _build_history_schedule(workout_history: list[dict]) -> str:
    """Format saved workout_history entries into a readable schedule block.

    Used for non-Garmin users. Multiple entries on the same date (double sessions)
    are shown on the same line separated by ' | '.
    """
    today = israel_now().date()
    by_date: dict[str, list[str]] = {}
    for entry in workout_history:
        d = entry.get("date", "")
        s = entry.get("summary", "")
        if d and s:
            by_date.setdefault(d, []).append(s)

    lines = []
    for offset in range(7, 0, -1):   # 7 days ago → yesterday
        day = today - timedelta(days=offset)
        day_name = f"{day.strftime('%a')} {day.day} {day.strftime('%b')}"
        sessions = by_date.get(day.isoformat(), [])
        if sessions:
            lines.append(f"  {day_name}: " + " | ".join(sessions))
        else:
            lines.append(f"  {day_name}: Rest")
    return "\n".join(lines) if lines else "  No workout history recorded yet"


def get_workout_recommendation(
    garmin_data: dict,
    user_profile: dict,
    weather: str = "",
    conversation_history: list[dict] | None = None,
    previous_workout: str | None = None,
    workout_history: list[dict] | None = None,
) -> dict:
    """
    Generate a personalised morning workout recommendation.

    Args:
        garmin_data:          Output of garmin.fetch_daily_stats(), or {} for non-Garmin users.
        user_profile:         User profile dict including coach_notes.
        weather:              One-line weather string (optional).
        conversation_history: Prior conversation messages — gives Claude context
                              about things discussed before /morning was called.
        previous_workout:     Yesterday's cached workout_recommendation text — lets
                              Claude know which muscle groups were trained last.
        workout_history:      Rolling list of {date, summary} dicts from storage.
                              Primary training history source for non-Garmin users;
                              supplemental context for Garmin users.

    Returns:
        {
            "summary":                str,
            "motivation":             str,
            "workout_recommendation": str,
            "recovery_tier":          str | None,  # None when no Garmin data
        }
    """
    has_garmin = bool(garmin_data)

    # ── 1. Classify recovery (rules only, Garmin users only) ─────────────────
    if has_garmin:
        sleep = garmin_data.get("sleep", {})
        hrv = garmin_data.get("hrv", {})
        recovery = classify_recovery(
            sleep_score=sleep.get("sleep_score"),
            hrv_last_night=hrv.get("last_night_avg"),
            hrv_weekly_avg=hrv.get("weekly_avg"),
        )
        tier: str | None = recovery["tier"]
    else:
        recovery = None
        tier = None

    # ── 2. Analyse workout history from Garmin (Garmin users only) ────────────
    week_analysis = analyze_week(garmin_data.get("recent_activities", [])) if has_garmin else None

    # ── 3. Assemble common prompt fields ─────────────────────────────────────
    name = user_profile.get("name", "Athlete")
    age = user_profile.get("age", "N/A")
    weight = user_profile.get("weight_kg", "N/A")
    level = user_profile.get("fitness_level", "intermediate")
    primary_goal = (
        user_profile.get("primary_goal")
        or user_profile.get("fitness_goal", "general fitness")
    )
    secondary_goal = user_profile.get("secondary_goal", "")
    gym_days = user_profile.get("weekly_gym_days", 0) or 0
    run_days = user_profile.get("weekly_run_days", 0) or 0
    # Legacy fallback: profiles created before the gym/run split used weekly_training_days
    if gym_days == 0 and run_days == 0:
        legacy_days = user_profile.get("weekly_training_days") or user_profile.get("workouts_per_week", 4)
        gym_days = int(legacy_days)
    total_weekly_days = gym_days + run_days
    session_duration = user_profile.get("preferred_session_duration_minutes", 60)
    event_blurb = _event_blurb(user_profile.get("target_event", {}))
    coach_notes = user_profile.get("coach_notes", [])
    mode = _sport_mode(user_profile)
    running_target_note = _running_target_blurb(user_profile)

    coach_notes_block = ""
    if coach_notes:
        notes_lines = "\n".join(f"  - {n.get('date', '?')}: {n.get('note', '')}" for n in coach_notes)
        coach_notes_block = f"\n## Coach Notes (long-term memory — respect these)\n{notes_lines}"

    previous_workout_block = ""
    if previous_workout:
        previous_workout_block = f"\n## Yesterday's Workout Plan (written by you)\n{previous_workout}\n"

    # ── 4. Build sport-specific context blocks ────────────────────────────────
    if mode == "run":
        training_days_line = f"Weekly runs: {run_days} | Gym: 0 | Preferred session: {session_duration} min"
    elif mode == "combined":
        training_days_line = (
            f"Weekly gym sessions: {gym_days} | Weekly runs: {run_days} | "
            f"Preferred session: {session_duration} min"
        )
    else:
        training_days_line = f"Weekly gym sessions: {gym_days} | Preferred session: {session_duration} min"

    # ── 5. Build the training history block (differs by Garmin availability) ──
    if has_garmin:
        hours_since = week_analysis["hours_since_last_workout"]
        hours_str = f"{hours_since:.0f} hours ago" if hours_since is not None else "unknown"
        consecutive = week_analysis["consecutive_training_days"]
        week_schedule = _build_week_schedule(week_analysis["daily_activity_map"])

        run_stats = ""
        if mode in ("run", "combined"):
            long_run = week_analysis.get("long_run_km_this_week", 0.0)
            run_sess = week_analysis.get("run_sessions_this_week", 0)
            run_stats = (
                f"\n- Run sessions: {run_sess} | Total km: {week_analysis['km_run_this_week']} km"
                f" | Longest run: {long_run} km"
            )

        training_context_block = f"""## Today's Recovery — {recovery["label"]}
{_TIER_CONTEXT[tier]}
Coaching note: {recovery["note"]}
Intensity ceiling: {recovery["intensity_ceiling"]} (max RPE {recovery["max_rpe"]}/10)

## Training — Last 7 Days (rolling, from Garmin)
{week_schedule}
- Total sessions: {week_analysis["total_sessions_this_week"]}
- Gym sessions: {week_analysis["gym_sessions_this_week"]}{run_stats}
- Consecutive training days: {consecutive}
- Last workout: {hours_str}"""

        intensity_ceiling_note = (
            f"recovery is very low.\n"
            f"Respect the intensity ceiling: {recovery['intensity_ceiling']} (max RPE {recovery['max_rpe']}/10)."
        )
        workout_rec_closer = "Close with a coaching note referencing today's recovery and load."
    else:
        history_schedule = _build_history_schedule(workout_history or [])
        training_context_block = f"""## No Biometric Data (Garmin not connected)
Base your rest/train decision purely on training load: consecutive days, session types, and muscle group overlap.

## Training — Last 7 Days (rolling, from saved history)
{history_schedule}"""

        intensity_ceiling_note = (
            "no biometric data is available.\n"
            "Default to moderate intensity unless the training load clearly calls for rest or active recovery."
        )
        workout_rec_closer = "Close with a brief coaching note on load or muscle group rationale."

    # ── 6. Build sport-specific task instructions ─────────────────────────────
    if mode == "gym":
        sport_task = (
            "This is a gym/strength athlete. "
            "If training: write a detailed gym session (exact exercises, sets × reps). "
            "Do NOT add effort cues per exercise. End the session with one coaching note "
            "on overall effort level for today (e.g. 'Push hard — last rep of each set should be a grind' "
            "or 'Keep it moderate — leave 2–3 reps in the tank throughout')."
        )
    elif mode == "run":
        sport_task = (
            "This is a running-focused athlete. "
            "If training: write a run session with clear structure (warmup / main set / cooldown). "
            "Use plain effort language — conversational, comfortable, comfortably hard, hard, all-out — "
            "not RPE numbers. Vary session type across the week — "
            "avoid scheduling the same type (e.g. long run) two days in a row. "
            "Common types: easy run, tempo, intervals, long run, recovery jog."
        )
    else:  # combined
        sport_task = (
            "This athlete does both gym and running. "
            f"Target {gym_days} gym session(s) and {run_days} run(s) per week. "
            "Check the week schedule above: if the run quota is unmet and legs are fresh, schedule a run; "
            "otherwise schedule a gym session. "
            "Do NOT schedule a hard leg session the day before a planned run, "
            "and avoid back-to-back long runs. "
            "If training gym: exact exercises, sets × reps. Do NOT add effort cues per exercise — "
            "end with one coaching note on overall effort for today. "
            "If training run: structure with plain effort language (conversational / comfortably hard / hard / all-out), "
            "vary type (easy / tempo / intervals / long)."
        )

    running_target_block = f"\n## Running Target\n{running_target_note}" if running_target_note else ""

    # ── 7. Assemble the full prompt ───────────────────────────────────────────
    prompt = f"""You are a personal fitness coach writing a morning workout recommendation.

## Athlete
- Name: {name}, Age: {age}, Weight: {weight} kg, Level: {level}
- Primary goal: {primary_goal}
{f"- Secondary goal: {secondary_goal}" if secondary_goal else ""}\
{f"- {event_blurb}" if event_blurb else ""}
- {training_days_line}{coach_notes_block}{running_target_block}

{training_context_block}
{previous_workout_block}
## Weather
{weather if weather else "N/A"}

## Your Task
{sport_task}

Decide whether {name} should train today, then write the morning briefing.

**Rest day decision:** You may recommend a full rest day when the training load warrants it —
for example, too many consecutive days relative to the target of {total_weekly_days} days/week with no break, or \
{intensity_ceiling_note}
Use the day-by-day schedule above and yesterday's workout to judge accumulated fatigue.
A rest day is the right call when training today would be counterproductive.

If training:
- summary: one sentence — workout type and effort level (e.g. "Tempo run 8 km — hard effort" or "Push day — heavy, ~60 min"). No RPE numbers.
- workout_recommendation: start directly with the workout title and structure (no greeting, no recovery recap). \
Full detailed session plan. {workout_rec_closer} Be specific:
  - Gym session → exact exercises, sets × reps per exercise
  - Run → distance, structure (warmup / main set / cooldown), pace guidance via plain effort words (conversational / comfortably hard / hard / all-out)
  - Active recovery → exactly what to do and for how long
  Target ~{session_duration} min total. Under 230 words.

If rest day:
- summary: one sentence — "Rest day — [brief reason]"
- workout_recommendation: 2–3 sentences — what to do instead and why this rest serves the goal.

- motivation: one sentence tied to the athlete's goal — works for both training and rest days.

Do not reference or react to any prior conversation in summary or motivation. Use prior context only in \
workout_recommendation if directly relevant (e.g. user mentioned soreness)."""

    # ── 8. Call Claude via brain.py (structured output) ──────────────────────
    result = get_workout_briefing(prompt, conversation_history)

    return {**result, "recovery_tier": tier}
