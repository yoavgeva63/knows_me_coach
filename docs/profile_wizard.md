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

All fields are required for `workout_recommender.py` to produce accurate output.

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

_finish_wizard()
    └── storage.save_profile(uid, profile)
    └── sends confirmation message
    └── context.user_data.clear()
    └── returns ConversationHandler.END
```

---

## Key Design Decisions

- **`context.user_data`** holds the in-progress profile dict during the wizard.
  It is only persisted to DynamoDB at `_finish_wizard()` — partial answers are not saved.
- **`skip_filled`** flag in `context.user_data` controls whether `_advance()` skips
  already-set fields. `True` for `/start`, `False` for `/profile`.
- **Inline keyboard states** (`WIZARD_GOAL`, `WIZARD_FITNESS_LEVEL`) only accept
  `CallbackQueryHandler` — a text message in these states is silently ignored
  (user must tap a button).
- `/cancel` at any point clears `context.user_data` and ends the conversation
  without saving anything.

---

## Callback Data Patterns

| Button | callback_data | Handler |
|---|---|---|
| Cut / Maintain / Bulk | `wiz:goal:<value>` | `wizard_goal_cb` |
| Beginner / Intermediate / Advanced | `wiz:fitness:<value>` | `wizard_fitness_cb` |
| None (dietary) | `wiz:dietary:None` | `wizard_dietary_cb` |
