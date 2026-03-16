"""
Weekly summary orchestrator.

Builds the full Weekly Review message for a Sun–Sat week and sends it.
Called by:
  - trigger_briefings.py  (automatic, every Saturday at 19:00 Israel time)
  - bot.py /weekly    (manual command)
  - bot.py tool loop  (when Claude calls trigger_weekly_briefing)

Message structure:
  📊 Weekly Review — Mar 9–15

  🏋️ Training  (X/Y gym · X/Y runs)
  <day-by-day list with ✅ / ✏️ / ❌ status>
  ─ Missed: ...

  🥗 Nutrition  (X/7 days logged)
  Avg X kcal · target Y
  Avg Xg protein · target Yg
       — or —
  Not enough data this week — log your meals daily ...

  😴 Recovery          ← Garmin users only
  Avg sleep Xh Ymin · score Z/100
  HRV <trend>
  Total steps N

  💬 Coach's Take
  <Claude insight>
"""
import asyncio
import logging
from datetime import date, timedelta

from telegram import Bot

import garmin
import storage
from brain import get_weekly_coaches_take
from briefing import md_to_html
from utils import israel_now

logger = logging.getLogger(__name__)

_STATUS_EMOJI = {"done": "✅", "modified": "✏️", "skipped": "❌"}
_LOW_NUTRITION_THRESHOLD = 3  # Days with logged meals needed to show averages.

# Days of the week for display (0=Mon in Python's weekday(), but our week starts Sunday).
_DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

# Whole words that identify a cardio/run session.  Checked as full words (split on
# whitespace) so that gym summaries like "burn fat — chest day" don't match.
# "km" keeps a substring check since it appears fused: "5km", "10km".
_CARDIO_WORDS = {"run", "runs", "running", "jog", "jogging", "sprint", "sprinting", "pace"}


def _is_cardio(summary: str) -> bool:
    """Return True if the summary looks like a cardio/run session rather than a gym session."""
    low = summary.lower()
    return bool(set(low.split()) & _CARDIO_WORDS) or "km" in low


# ---------------------------------------------------------------------------
# Week boundary helpers
# ---------------------------------------------------------------------------

def _get_week_bounds() -> tuple[date, date]:
    """Return (sunday, saturday) for the current Sun–Sat week in Israel time.

    Uses Israel local time rather than server UTC so that late-night calls
    (e.g. after midnight UTC / 02:00 AM Israel) still return the correct week.
    """
    now_israel = israel_now()
    today = now_israel.date()
    # Python weekday(): Mon=0 … Sun=6.  Days since Sunday = (weekday + 1) % 7.
    days_since_sunday = (today.weekday() + 1) % 7
    sunday = today - timedelta(days=days_since_sunday)
    saturday = sunday + timedelta(days=6)
    return sunday, saturday


def _date_label(d: date) -> str:
    """Return the three-letter day name for a date."""
    return _DAY_NAMES[d.weekday()]  # Mon–Sat = 0–5, Sun = 6 → maps to index 6


def _week_header(sunday: date, saturday: date) -> str:
    """Return the formatted date range header, e.g. 'Mar 9–15'."""
    if sunday.month == saturday.month:
        return f"{sunday.strftime('%b')} {sunday.day}–{saturday.day}"
    return f"{sunday.strftime('%b')} {sunday.day}–{saturday.strftime('%b')} {saturday.day}"


# ---------------------------------------------------------------------------
# Section builders
# ---------------------------------------------------------------------------

def _build_training_block(
    workout_history: list[dict],
    sunday: date,
    saturday: date,
    weekly_gym_days: int,
    weekly_run_days: int,
) -> str:
    """Build the 🏋️ Training section."""
    # Filter entries to this week.
    week_entries = [
        e for e in workout_history
        if sunday.isoformat() <= e.get("date", "") <= saturday.isoformat()
    ]
    # Sort chronologically.
    week_entries.sort(key=lambda e: e["date"])

    lines = []
    gym_done = 0
    runs_done = 0
    missed_days: list[str] = []

    for entry in week_entries:
        entry_date = date.fromisoformat(entry["date"])
        day_label = _date_label(entry_date)
        status = entry.get("status")
        emoji = _STATUS_EMOJI.get(status, "")

        # Determine the display summary.
        if status == "modified":
            display = entry.get("actual_summary") or entry.get("summary", "")
        else:
            display = entry.get("summary", "")

        if status == "done":
            lines.append(f"{day_label}: {emoji} {display}")
            if _is_cardio(display):
                runs_done += 1
            else:
                gym_done += 1
        elif status == "modified":
            actual_type = entry.get("actual_type", "same_modified")
            tag = "modified" if actual_type == "same_modified" else "instead"
            lines.append(f"{day_label}: {emoji} {display} ({tag})")
            if _is_cardio(display):
                runs_done += 1
            else:
                gym_done += 1
        elif status == "skipped":
            missed_days.append(day_label)
            lines.append(f"{day_label}: {emoji} Skipped")
        else:
            # No status logged — assume done (user didn't report a skip or modification).
            lines.append(f"{day_label}: {_STATUS_EMOJI['done']} {display}")
            if _is_cardio(display):
                runs_done += 1
            else:
                gym_done += 1

    header = f"🏋️ Training  ({gym_done}/{weekly_gym_days} gym · {runs_done}/{weekly_run_days} runs)"

    body = "\n".join(lines) if lines else "No workouts logged this week."

    missed_line = ""
    if missed_days:
        missed_line = f"\n─ Missed: {', '.join(missed_days)}"

    return f"{header}\n{body}{missed_line}"


def _build_nutrition_block(
    nutrition_meals: dict[str, list[dict]],
    sunday: date,
    saturday: date,
    target_kcal: int | None,
    target_protein: int | None,
) -> str:
    """Build the 🥗 Nutrition section."""
    # Filter to this week's dates.
    week_dates = [
        (sunday + timedelta(days=i)).isoformat()
        for i in range(7)
    ]
    days_data = {d: nutrition_meals.get(d, []) for d in week_dates if d in nutrition_meals}
    days_logged = len(days_data)

    if days_logged < _LOW_NUTRITION_THRESHOLD:
        return (
            "🥗 Nutrition\n"
            "Not enough data this week — log your meals daily to unlock nutrition insights."
        )

    total_kcal = 0
    total_protein = 0
    for meals in days_data.values():
        for meal in meals:
            total_kcal += meal.get("kcal", 0)
            total_protein += meal.get("protein_g", 0)

    avg_kcal = round(total_kcal / days_logged)
    avg_protein = round(total_protein / days_logged)

    kcal_line = f"Avg {avg_kcal} kcal"
    if target_kcal:
        kcal_line += f" · target {target_kcal}"

    protein_line = f"Avg {avg_protein}g protein"
    if target_protein:
        protein_line += f" · target {target_protein}g"

    return (
        f"🥗 Nutrition  ({days_logged}/7 days logged)\n"
        f"{kcal_line}\n"
        f"{protein_line}"
    )


def _build_recovery_block(garmin_week: dict) -> str:
    """Build the 😴 Recovery section from aggregated Garmin week data.

    Args:
        garmin_week: Dict with optional keys: avg_sleep_duration_min, avg_sleep_score,
                     hrv_trend, total_steps, days_with_sleep_data.

    Returns:
        Formatted recovery block string, or empty string if no data.
    """
    if not garmin_week:
        return ""

    lines = []

    avg_score = garmin_week.get("avg_sleep_score")
    avg_dur = garmin_week.get("avg_sleep_duration_min")
    days_with_sleep = garmin_week.get("days_with_sleep_data")
    if avg_dur is not None:
        hours, mins = divmod(round(avg_dur), 60)
        sleep_str = f"Avg sleep {hours}h {mins}min"
        if avg_score is not None:
            sleep_str += f" · score {round(avg_score)}/100"
        if days_with_sleep is not None and days_with_sleep < 7:
            sleep_str += f"  ({days_with_sleep}/7 nights)"
        lines.append(sleep_str)

    hrv_trend = garmin_week.get("hrv_trend")
    if hrv_trend:
        lines.append(f"HRV {hrv_trend}")

    total_steps = garmin_week.get("total_steps")
    if total_steps is not None:
        lines.append(f"Total steps {total_steps:,}")

    if not lines:
        return ""

    return "😴 Recovery\n" + "\n".join(lines)


# ---------------------------------------------------------------------------
# Garmin week aggregation
# ---------------------------------------------------------------------------

def _fetch_garmin_week(user_id_str: str, sunday: date, saturday: date, has_garmin: bool) -> dict:
    """Fetch the aggregated Garmin health summary for the Sun–Sat week.

    Makes ~15 API calls (7 sleep + 7 steps + 1 HRV). Each sub-call catches its
    own errors so a single failing endpoint never blocks the weekly summary.
    Returns {} if the user has no Garmin connected or all calls fail.

    Args:
        user_id_str: Telegram user ID string.
        sunday:      First day of the week.
        saturday:    Last day of the week.
        has_garmin:  Whether the user has Garmin tokens stored.
    """
    if not has_garmin:
        return {}
    try:
        return garmin.fetch_week_stats(user_id_str, sunday, saturday)
    except Exception as exc:
        logger.warning("fetch_week_stats failed for %s: %s", user_id_str, exc)
        return {}


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

async def send_weekly_briefing(
    bot: Bot,
    chat_id: int,
    user_id_str: str,
) -> None:
    """Build and send the weekly summary message.

    Computes the current Sun–Sat week bounds, gathers all relevant data,
    builds each section, calls Claude for the Coach's Take, and sends one message.
    Marks the weekly summary as sent so the auto-trigger doesn't double-send.
    """
    sunday, saturday = _get_week_bounds()
    header = _week_header(sunday, saturday)

    profile = storage.load_profile(user_id_str)
    if not profile:
        await bot.send_message(chat_id, "No profile found — run /start to set up first.")
        return

    workout_history: list[dict] = profile.get("workout_history", [])
    nutrition_meals: dict = profile.get("nutrition", {}).get("daily_meals", {})
    coach_notes: list[dict] = profile.get("coach_notes", [])
    weekly_gym_days: int = profile.get("weekly_gym_days", 3)
    weekly_run_days: int = profile.get("weekly_run_days", 0)

    # Target macros from profile (set by profile_wizard / nutrition module).
    target_kcal: int | None = profile.get("target_kcal")
    target_protein: int | None = profile.get("target_protein_g")

    # Build structured sections.
    training_block = _build_training_block(
        workout_history, sunday, saturday, weekly_gym_days, weekly_run_days
    )
    nutrition_block = _build_nutrition_block(
        nutrition_meals, sunday, saturday, target_kcal, target_protein
    )
    has_garmin = bool(profile.get("garmin_tokens"))
    loop = asyncio.get_running_loop()
    garmin_week = await loop.run_in_executor(
        None,
        lambda: _fetch_garmin_week(user_id_str, sunday, saturday, has_garmin),
    )
    recovery_block = _build_recovery_block(garmin_week)

    # This week's chat history for Coach's Take context (filter by ts >= sunday).
    full_history = storage.load_history(user_id_str)
    week_chat = [
        m for m in full_history
        if m.get("ts", "") >= sunday.isoformat()
    ]

    # Last week comparison — only if we have enough data.
    last_sunday = sunday - timedelta(days=7)
    last_saturday = saturday - timedelta(days=7)
    last_week_entries = [
        e for e in workout_history
        if last_sunday.isoformat() <= e.get("date", "") <= last_saturday.isoformat()
    ]
    comparison_line = ""
    # Count as done: any entry that isn't explicitly skipped (mirrors the training block logic).
    last_done = sum(1 for e in last_week_entries if e.get("status") != "skipped")
    this_done = sum(
        1 for e in workout_history
        if sunday.isoformat() <= e.get("date", "") <= saturday.isoformat()
        and e.get("status") != "skipped"
    )
    if last_week_entries and (last_done > 0 or this_done > 0):
        diff = this_done - last_done
        if diff > 0:
            comparison_line = f"vs last week: +{diff} session{'s' if diff != 1 else ''} 📈"
        elif diff < 0:
            comparison_line = f"vs last week: {diff} session{'s' if diff != -1 else ''} 📉"
        else:
            comparison_line = "vs last week: same volume ➡️"

    # Call Claude for the Coach's Take.
    try:
        coaches_take = await loop.run_in_executor(
            None,
            lambda: get_weekly_coaches_take(
                training_block, nutrition_block, recovery_block, week_chat, coach_notes
            ),
        )
    except Exception as exc:
        logger.error("get_weekly_coaches_take failed for %s: %s", user_id_str, exc)
        coaches_take = "Couldn't generate the Coach's Take right now — check back in a moment."

    # Assemble the final message.
    sections = [f"📊 Weekly Review — {header}", "", training_block, "", nutrition_block]
    if recovery_block:
        sections += ["", recovery_block]
    if comparison_line:
        sections += ["", comparison_line]
    sections += ["", f"💬 Coach's Take\n{coaches_take}"]

    message = "\n".join(sections)

    await bot.send_message(chat_id, md_to_html(message), parse_mode="HTML")

    storage.mark_weekly_sent(user_id_str, saturday.isoformat())
    logger.info("Weekly summary sent and marked for week ending %s.", saturday.isoformat())
