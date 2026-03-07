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
| `primary_goal` | str | Cut / Maintain / Bulk | inline keyboard |
| `fitness_level` | str | Beginner / Intermediate / Advanced | inline keyboard |
| `weekly_training_days` | int | 1–7 | free text |
| `preferred_session_duration_minutes` | int | 10–300 | free text |
| `dietary_restrictions` | str | none | free text or "None" button |
| `garmin_asked` | bool | — | inline keyboard (Yes/No) |
| `morning_alarm_time` | str | HH:MM or "sleep" | inline keyboard or free text |

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
- **`WIZARD_GARMIN_EMAIL` / `WIZARD_GARMIN_PASSWORD`** are NOT in `_FIELD_STATES` — they are
  only reachable via explicit redirect from `wizard_garmin_yes_cb`, not via `_advance()`.
- **Inline keyboard states** (`WIZARD_GOAL`, `WIZARD_FITNESS_LEVEL`, `WIZARD_GARMIN`,
  `WIZARD_ALARM_TIME`) also accept text input where applicable.
- `/cancel` at any point clears `context.user_data` and ends the conversation
  without saving anything.

---

## Callback Data Patterns

| Button | callback_data | Handler |
|---|---|---|
| Cut / Maintain / Bulk | `wiz:goal:<value>` | `wizard_goal_cb` |
| Beginner / Intermediate / Advanced | `wiz:fitness:<value>` | `wizard_fitness_cb` |
| None (dietary) | `wiz:dietary:None` | `wizard_dietary_cb` |
| Yes / No (Garmin) | `wiz:garmin:yes` / `wiz:garmin:no` | `wizard_garmin_yes_cb` / `wizard_garmin_no_cb` |
| 07:00 / 07:30 / 08:00 / Sleep mode | `wiz:alarm:<value>` | `wizard_alarm_time_cb` |
