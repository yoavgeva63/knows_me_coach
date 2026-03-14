# Profile Wizard

## Overview

Implemented in `profile_wizard.py` as a PTB `ConversationHandler`.
Registered in `bot.py` via `build_wizard_handler()`.

Two entry points with different skip behaviour:
- `/start` — skips fields already set in DynamoDB (first-run only fills what's missing)
- `/profile` — asks every field regardless (full update flow)

---

## Fields Collected (in order)

| Profile key | Type | Validation | Input method |
|---|---|---|---|
| `name` | str | none | free text |
| `age` | int | 8–100 | free text |
| `weight_kg` | float | 20–300 | free text |
| `height_cm` | int | 100–220 | free text |
| `sex` | str | male / female | inline keyboard |
| `primary_goal` | str | see Goal Picker below | inline keyboard + optional sub-state |
| `fitness_level` | str | Beginner / Intermediate / Advanced | inline keyboard |
| `weekly_gym_days` | int | 0–5 | inline keyboard |
| `weekly_run_days` | int | 0–5 | inline keyboard |
| `preferred_session_duration_minutes` | int | 10–300 | free text |
| `dietary_restrictions` | str | none | free text or "None" button |
| `garmin_asked` | bool | — | inline keyboard (Yes/No) |
| `morning_alarm_time` | str | HH:MM or "sleep" | inline keyboard or free text |

**Running-specific fields** (only set when the user picks "Running goal"):

| Profile key | Type | Notes |
|---|---|---|
| `running_target_km` | float | Target race distance in km (e.g. 21.0). Used by `workout_recommender.py` to set periodization context. |

---

## Goal Picker

The goal question shows three rows of buttons:

```
[Cut]  [Maintain]  [Bulk]
[🏃 Running goal]  [Run + Gym]
[General fitness]  [✏️ Custom]
```

- **Cut / Maintain / Bulk / Run + Gym / General fitness** → stored directly as `primary_goal`, wizard advances.
- **🏃 Running goal** → opens `WIZARD_GOAL_RUNNING_DISTANCE` sub-state:
  - Asks "target race distance in km?" (free text, e.g. "21")
  - Stores `running_target_km` (float) and `primary_goal` (e.g. `"21km race"`)
  - Then advances normally.
- **✏️ Custom** → opens `WIZARD_GOAL_CUSTOM` sub-state:
  - Accepts any free text, stored as `primary_goal`.
  - Then advances normally.

`WIZARD_GOAL_RUNNING_DISTANCE` and `WIZARD_GOAL_CUSTOM` are **not** in `_FIELD_STATES` — they are only reachable via the goal button, not via `_advance()`.

---

## Training Day Split

Replaces the former `weekly_training_days` single field. Two sequential questions:

1. "How many gym / strength sessions per week?" — 0–5 inline buttons → `weekly_gym_days`
2. "How many runs per week?" — 0–5 inline buttons → `weekly_run_days`

`workout_recommender.py` uses these to derive **sport mode** (`gym` / `run` / `combined`) and generates sport-appropriate workout plans.

Legacy profiles that still have `weekly_training_days` (and no `weekly_gym_days`) are handled gracefully in the recommender by treating the legacy value as `weekly_gym_days`.

---

## State Machine

```
/start or /profile
    └── _advance(after_field=None)
            └── finds first unfilled field (or all fields if skip_filled=False)
                └── calls _ASK_FNS[state](update, context) → returns state int

User answers
    └── wizard_<field>(update, context)
            ├── validates input (re-asks on invalid)
            ├── saves to context.user_data["profile"][field]
            └── _advance(after_field=<field>)
                    └── finds next unfilled field, or calls _finish_wizard()

Special branch — Running goal:
    WIZARD_GOAL (🏃 button) → WIZARD_GOAL_RUNNING_DISTANCE (text) → _advance(after_field="primary_goal")

Special branch — Custom goal:
    WIZARD_GOAL (✏️ button) → WIZARD_GOAL_CUSTOM (text) → _advance(after_field="primary_goal")

Special branch — Garmin Yes:
    WIZARD_GARMIN (yes) → WIZARD_GARMIN_EMAIL → WIZARD_GARMIN_PASSWORD
        └── on success/fail → _ask_alarm_time() → WIZARD_ALARM_TIME
    WIZARD_GARMIN (no)  → _advance() → WIZARD_ALARM_TIME

_finish_wizard()
    └── storage.save_profile(uid, profile)
    └── sends confirmation message (with /connect_garmin note if Garmin linking failed)
    └── context.user_data.clear()
    └── returns ConversationHandler.END
```

---

## Key Design Decisions

- **`context.user_data`** holds the in-progress profile dict during the wizard.
  It is only persisted to DynamoDB at `_finish_wizard()` — partial answers are not saved.
  Exception: Garmin tokens are saved immediately on successful auth (inside `wizard_garmin_password`).
- **`skip_filled`** flag in `context.user_data` controls whether `_advance()` skips
  already-set fields. `True` for `/start`, `False` for `/profile`.
- **`WIZARD_GARMIN_EMAIL` / `WIZARD_GARMIN_PASSWORD`** and the goal sub-states are NOT in
  `_FIELD_STATES` — only reachable via explicit redirect, not via `_advance()`.
- `/cancel` at any point clears `context.user_data` and ends the conversation
  without saving anything.

---

## Callback Data Patterns

| Button | callback_data | Handler |
|---|---|---|
| Cut / Maintain / Bulk / Run + Gym / General fitness | `wiz:goal:<value>` | `wizard_goal_cb` |
| 🏃 Running goal | `wiz:goal:_running` | `wizard_goal_cb` → redirects to sub-state |
| ✏️ Custom | `wiz:goal:_custom` | `wizard_goal_cb` → redirects to sub-state |
| Beginner / Intermediate / Advanced | `wiz:fitness:<value>` | `wizard_fitness_cb` |
| 0–5 (gym days) | `wiz:gym_days:<n>` | `wizard_gym_days_cb` |
| 0–5 (run days) | `wiz:run_days:<n>` | `wizard_run_days_cb` |
| None (dietary) | `wiz:dietary:None` | `wizard_dietary_cb` |
| Yes / No (Garmin) | `wiz:garmin:yes` / `wiz:garmin:no` | `wizard_garmin_yes_cb` / `wizard_garmin_no_cb` |
| 07:00 / 08:00 / 09:00 / Sleep mode | `wiz:alarm:<value>` | `wizard_alarm_time_cb` |
