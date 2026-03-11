# Briefing System

## Overview

The morning briefing is the bot's main daily touch-point. It is sent automatically
via cron (`morning_check.py`) or manually via `/morning`. It pre-generates the full
workout plan and caches it so button taps respond instantly.

Garmin is optional. Users without a connected Garmin account get the same briefing
minus the recovery line; workout decisions are based on the `workout_history` rolling log.

---

## Flow

```
/morning  (or cron trigger)
    │
    ├── fetch_weather()                          [briefing.py]
    ├── storage.load_profile()
    ├── storage.load_history()
    │
    ├── [if garmin_tokens present]
    │       garmin.fetch_daily_stats()           [Garmin API]
    │
    ├── workout_recommender.get_workout_recommendation()
    │       ├── [Garmin path]
    │       │     ├── recovery.classify_recovery()   [pure rules → tier]
    │       │     ├── garmin.analyze_week()           [rolling 7-day activity summary]
    │       │     └── builds prompt with recovery + Garmin schedule
    │       ├── [no-Garmin path]
    │       │     └── builds prompt with workout_history schedule (no recovery block)
    │       └── brain.get_workout_briefing()     [Claude API — structured output]
    │               returns: {summary, motivation, workout_recommendation, recovery_tier}
    │
    ├── storage.save_daily_workout_and_history() [one DynamoDB write]
    │       saves daily_workout cache + appends summary to workout_history
    │
    └── bot.send_message(briefing_text, reply_markup=_BRIEFING_KEYBOARD)
            Garmin:    "Good morning! 🌅\n{emoji} {recovery_line}\n{summary}\n{motivation}"
            no-Garmin: "Good morning! 🌅\n{summary}\n{motivation}"
```

---

## Briefing Message Format

**With Garmin:**
```
Good morning Yoav! 🌅
🟢 Recovery looks solid today — sleep at 84/100, HRV above baseline.
Push day — RPE 7, heavy compound work (~60 min).
💪 You've trained 4 days straight this week — one more solid push.

[💪 Workout]  [🥗 Nutrition]
[😴 Sleep]    [💧 Hydration]
```

**Without Garmin:**
```
Good morning Yoav! 🌅
Push day — RPE 7, heavy compound work (~60 min).
💪 You've trained 4 days straight this week — one more solid push.

[💪 Workout]  [🥗 Nutrition]
[😴 Sleep]    [💧 Hydration]
```

---

## Inline Keyboard Buttons

All buttons use `callback_data="action:<name>"` and are handled by
`handle_briefing_action()` in `bot.py` via `CallbackQueryHandler(pattern=r"^action:")`.

| Button | callback_data | Current behaviour |
|---|---|---|
| 💪 Workout | `action:workout` | Loads `workout_recommendation` from DynamoDB cache, sends instantly |
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
    "workout_recommendation": "...",
    "recovery_tier": "high"
  }
}
```

`recovery_tier` is `null` for non-Garmin users.

`storage.load_daily_workout(user_id, today)` returns `None` if date doesn't match
(i.e. stale from yesterday) — the Workout button then asks the user to `/morning`.

---

## Workout History Log

A rolling log of the last 14 entries (7 days × 2 sessions buffer) stored in the profile:

```json
{
  "workout_history": [
    {"date": "2026-03-09", "summary": "Push day — RPE 7, heavy compound work (~60 min)"},
    {"date": "2026-03-10", "summary": "Run — RPE 5, easy 6 km"},
    {"date": "2026-03-10", "summary": "Evening mobility — 20 min"}
  ]
}
```

Written by `storage.save_daily_workout_and_history()` after every morning briefing.
Read by `workout_recommender._build_history_schedule()` for non-Garmin users.
Also passed to Garmin users as supplemental context.

---

## Claude Structured Output (brain.py)

`get_workout_briefing(prompt, conversation_history)` uses forced tool use:
- Tool: `workout_briefing` with schema `{summary, motivation, workout_recommendation}`
- `tool_choice={"type": "tool", "name": "workout_briefing"}` — Claude cannot output free text
- Returns `response.content[0].input` — a guaranteed dict, no parsing needed

---

## Recovery Tiers

Defined in `recovery.py`, used for emoji and label in the briefing (Garmin users only):

| Tier | Emoji | Label |
|---|---|---|
| high | 🟢 | Recovery looks solid today |
| moderate | 🟡 | Recovery is decent today |
| low | 🟠 | Recovery is a bit low today |
| very_low | 🔴 | Recovery is very low today |
