# Briefing System

## Overview

The morning briefing is the bot's main daily touch-point. It is sent automatically
via cron (`morning_check.py`) or manually via `/morning`. It pre-generates the full
workout plan and caches it so button taps respond instantly.

---

## Flow

```
/morning  (or cron trigger)
    │
    ├── fetch_weather()                          [briefing.py]
    ├── storage.load_profile()
    ├── storage.load_history()
    ├── garmin_daily_stats.fetch_daily_stats()   [Garmin API]
    │
    ├── workout_recommender.get_workout_recommendation()
    │       ├── recovery.classify_recovery()     [pure rules → tier]
    │       ├── garmin_activity_analyzer.analyze_week()
    │       ├── builds prompt string
    │       └── brain.get_workout_briefing()     [Claude API — structured output]
    │               returns: {summary, motivation, full_recommendation}
    │
    ├── storage.save_daily_workout()             [cached in DynamoDB profile]
    │
    └── bot.send_message(briefing_text, reply_markup=_BRIEFING_KEYBOARD)
            briefing_text = "{emoji} {recovery_line}\n{summary}\n{motivation}"
```

---

## Briefing Message Format

```
🟢 Recovery looks solid today — sleep at 84/100, HRV above baseline.
Push day — RPE 7, heavy compound work (~60 min).
You've trained 4 days straight this week — one more solid push.

[💪 Workout]  [🥗 Nutrition]
[😴 Sleep]    [💧 Hydration]
```

**Lines:**
1. Recovery emoji + `_build_recovery_line()` result (rule-based, no Claude)
2. `summary` from Claude structured output
3. `motivation` from Claude structured output

---

## Inline Keyboard Buttons

All buttons use `callback_data="action:<name>"` and are handled by
`handle_briefing_action()` in `bot.py` via `CallbackQueryHandler(pattern=r"^action:")`.

| Button | callback_data | Current behaviour |
|---|---|---|
| 💪 Workout | `action:workout` | Loads `full_recommendation` from DynamoDB cache, sends instantly |
| 🥗 Nutrition | `action:nutrition` | Placeholder — to be implemented |
| 😴 Sleep | `action:sleep` | Placeholder — to be implemented |
| 💧 Hydration | `action:hydration` | Placeholder — to be implemented |

---

## Workout Cache

Stored inside the user's profile record in DynamoDB (`fitness_coach_users` table):

```json
{
  "daily_workout": {
    "date": "2026-03-04",
    "summary": "Push day — RPE 7, heavy compound work (~60 min)",
    "motivation": "...",
    "full_recommendation": "Good morning Yoav! ...",
    "recovery_tier": "high"
  }
}
```

`storage.load_daily_workout(user_id, today)` returns `None` if date doesn't match
(i.e. stale from yesterday) — the Workout button then asks the user to `/morning`.

---

## Claude Structured Output (brain.py)

`get_workout_briefing(prompt, conversation_history)` uses forced tool use:
- Tool: `workout_briefing` with schema `{summary, motivation, full_recommendation}`
- `tool_choice={"type": "tool", "name": "workout_briefing"}` — Claude cannot output free text
- Returns `response.content[0].input` — a guaranteed dict, no parsing needed

---

## Recovery Tiers

Defined in `recovery.py`, used for emoji and label in the briefing:

| Tier | Emoji | Label |
|---|---|---|
| high | 🟢 | Recovery looks solid today |
| moderate | 🟡 | Recovery is decent today |
| low | 🟠 | Recovery is a bit low today |
| very_low | 🔴 | Recovery is very low today |
