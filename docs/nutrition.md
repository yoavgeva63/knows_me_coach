# Nutrition System

## Overview

The nutrition flow lets users see daily macro targets, get Claude-generated meal suggestions per slot (breakfast / lunch / dinner), log meals, and plan meals around available ingredients.

Entry point: the **🥗 Nutrition** button in the morning briefing keyboard (`action:nutrition`).

---

## File Map

| File | Role |
|---|---|
| `nutrition.py` | Macro formula, remaining calc, message formatters, Claude prompt builders |
| `brain.py` | `get_meal_suggestions()`, `get_ingredient_meal()` — both use forced tool use |
| `storage.py` | `load/save_daily_meal`, `load/save_groceries`, `save/load/clear_pending_meal_options`, `replace_daily_meal` |
| `bot.py` | `handle_nutrition_callback()`, `_build_nutrition_ingredient_handler()`, `_nutr_ingredient_entry()` |

---

## DynamoDB Schema

All nutrition data lives under `profile["nutrition"]` in the `fitness_coach_users` table (same item as the rest of the profile).

```json
{
  "nutrition": {
    "daily_meals": {
      "2026-03-08": [
        {
          "name": "Greek Yogurt Bowl",
          "slot": "breakfast",
          "kcal": 480,
          "protein_g": 42,
          "fat_g": 12,
          "carbs_g": 52,
          "logged_at": "2026-03-08T08:31:00Z"
        }
      ]
    },
    "last_groceries": "eggs, chicken, oats, spinach",
    "last_groceries_updated_at": "2026-03-06T10:00:00Z",
    "pending_options": {
      "date": "2026-03-08",
      "slot": "lunch",
      "options": [ { ... }, { ... } ]
    }
  }
}
```

`daily_meals` is pruned to the last 7 days on every `save_daily_meal` call.
`pending_options` holds the most recent Claude-generated options so buttons can log by index (Telegram callback_data is limited to 64 bytes).

---

## Macro Formula

Uses **Mifflin-St Jeor** for BMR, requiring `weight_kg`, `height_cm`, and `age` from the profile:

```
BMR (male)   = 10×weight + 6.25×height − 5×age + 5
BMR (female) = 10×weight + 6.25×height − 5×age − 161
TDEE         = BMR × activity_multiplier
Calories     = TDEE + goal_delta
```

| Goal | Calorie delta | Protein factor |
|---|---|---|
| Cut | −300 kcal | 2.2 g/kg |
| Maintain | 0 | 1.8 g/kg |
| Bulk | +300 kcal | 2.0 g/kg |

Fat = 25% of calories ÷ 9. Carbs fill the remainder.

Falls back to `{kcal: 2000, protein: 150, fat: 56, carbs: 213}` if profile fields are missing.

---

## Callback Routing (`^nutr:` pattern)

All handled by `handle_nutrition_callback()` in `bot.py`:

| Callback data | Action |
|---|---|
| `nutr:show` | Show macro dashboard |
| `nutr:meal:<slot>` | Generate 2 meal options via Claude |
| `nutr:log:<index>` | Log pending option 0 or 1 |
| `nutr:more:<slot>` | Regenerate 2 options for same slot |
| `nutr:ingredients:<slot>` | Enter ingredient flow (ConversationHandler entry) |
| `nutr:confirm_groceries:<slot>` | Reuse saved grocery list → Claude |
| `nutr:new_groceries:<slot>` | Clear saved list and prompt for new one |
| `nutr:day` | Full day summary |
| `nutr:shopping:<slot>` | Placeholder (shopping list — not yet implemented) |

---

## Ingredient Flow (ConversationHandler)

State: `_NUTR_INGREDIENTS` (single state, `_build_nutrition_ingredient_handler()`).

```
[🥕 Use my ingredients] tapped
       ↓
_nutr_ingredient_entry() (CallbackQueryHandler entry point)
       ↓
groceries saved within 3 days?
  YES → ask "Same or update?" → two inline buttons
          confirm_groceries → Claude → meal
          new_groceries     → prompt text → next message
  NO  → prompt text → _NUTR_INGREDIENTS state
       ↓
_nutr_receive_ingredients() — saves groceries, calls Claude, sends meal
```

Slot is stored in `context.user_data["nutr_slot"]` for the duration of the ConversationHandler.

---

## Claude API Calls

Both use `tool_choice={"type": "tool", "name": "..."}` (forced tool use) — no free-text macro parsing.

- `get_meal_suggestions(prompt)` → `_MEAL_SUGGESTION_TOOL` → list of 2 meal dicts
- `get_ingredient_meal(prompt)` → `_INGREDIENT_MEAL_TOOL` → 1 meal dict

Prompts are assembled in `nutrition.py` (`build_meal_suggestion_prompt`, `build_ingredient_meal_prompt`) and include: daily targets, remaining macros, goal, dietary restrictions, already-logged meals.
