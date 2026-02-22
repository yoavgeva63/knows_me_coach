"""
Recovery classifier — pure logic, no AI.

Converts raw Garmin sleep score and HRV data into a structured recovery tier
that the workout recommender uses to constrain Claude's output.

HRV note: Garmin reports HRV in milliseconds (e.g. 45 ms), not 0–100.
We classify by comparing last night's HRV to the 7-day baseline (weekly_avg).
A ratio >= 1.0 means fully recovered; lower ratios mean increasing suppression.
"""


def classify_recovery(
    sleep_score: int | None,
    hrv_last_night: float | None,
    hrv_weekly_avg: float | None,
) -> dict:
    """
    Classify today's recovery status from Garmin data.

    Args:
        sleep_score:     0–100 overall sleep score from Garmin (None if unavailable).
        hrv_last_night:  Last night's average HRV in ms (None if unavailable).
        hrv_weekly_avg:  7-day average HRV in ms (None if unavailable).

    Returns:
        {
            "tier":              "high" | "moderate" | "low" | "very_low",
            "label":             str,   # human-readable tier name
            "intensity_ceiling": str,   # plain-English cap for Claude
            "max_rpe":           int,   # 1–10 RPE ceiling
            "note":              str,   # one-sentence coaching note
        }
    """
    # ── HRV dimension ────────────────────────────────────────────────────────
    # Score: 2 = at/above baseline, 1 = slightly below, 0 = notably below, -1 = suppressed
    if hrv_last_night is not None and hrv_weekly_avg and hrv_weekly_avg > 0:
        ratio = hrv_last_night / hrv_weekly_avg
        if ratio >= 0.95:
            hrv_points = 2
        elif ratio >= 0.85:
            hrv_points = 1
        elif ratio >= 0.75:
            hrv_points = 0
        else:
            hrv_points = -1
    else:
        hrv_points = None  # data unavailable

    # ── Sleep dimension ───────────────────────────────────────────────────────
    # Score mirrors HRV scale
    if sleep_score is not None:
        if sleep_score >= 70:
            sleep_points = 2
        elif sleep_score >= 55:
            sleep_points = 1
        elif sleep_score >= 40:
            sleep_points = 0
        else:
            sleep_points = -1
    else:
        sleep_points = None  # data unavailable

    # ── Combined score ────────────────────────────────────────────────────────
    # When one dimension is missing, use only the other (conservative default).
    if hrv_points is not None and sleep_points is not None:
        total = hrv_points + sleep_points
    elif sleep_points is not None:
        total = sleep_points       # only sleep: cap at moderate (max 2)
    elif hrv_points is not None:
        total = hrv_points         # only HRV: cap at moderate (max 2)
    else:
        total = 0                  # no data: default to moderate-low

    # ── Tier lookup ───────────────────────────────────────────────────────────
    if total >= 3:
        return {
            "tier": "high",
            "label": "Full Recovery",
            "intensity_ceiling": "high intensity",
            "max_rpe": 9,
            "note": "Green day — HRV and sleep are both strong. Push hard.",
        }
    elif total >= 1:
        return {
            "tier": "moderate",
            "label": "Moderate Recovery",
            "intensity_ceiling": "moderate intensity",
            "max_rpe": 7,
            "note": "Decent recovery. Train normally but don't go to absolute failure.",
        }
    elif total >= -1:
        return {
            "tier": "low",
            "label": "Low Recovery",
            "intensity_ceiling": "low to moderate intensity",
            "max_rpe": 5,
            "note": "Body is under stress. Keep the session honest and short.",
        }
    else:
        return {
            "tier": "very_low",
            "label": "Very Low Recovery",
            "intensity_ceiling": "active recovery only",
            "max_rpe": 3,
            "note": "Prioritise rest. Light movement only — a hard session today would dig a deeper hole.",
        }
