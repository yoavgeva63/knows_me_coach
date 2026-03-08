"""
Nutrition planning module for the fitness coach bot.

Responsibilities:
- Calculate daily macro targets from the user profile (Mifflin-St Jeor TDEE).
- Compute remaining macros from today's logged meals.
- Build Claude prompts for meal suggestion and ingredient-based meal requests.
- Format all nutrition-related Telegram messages and inline keyboards.

Does NOT call Claude directly — all LLM calls go through brain.py.
Does NOT touch DynamoDB directly — all persistence goes through storage.py.
"""
import logging
from datetime import datetime, timezone

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Macro calculation
# ---------------------------------------------------------------------------

_ACTIVITY_MULTIPLIER = {
    "Beginner":     1.375,  # light exercise 1–3 days/week
    "Intermediate": 1.55,   # moderate exercise 3–5 days/week
    "Advanced":     1.725,  # hard exercise 6–7 days/week
}

_GOAL_CALORIE_DELTA = {
    "Cut":      -300,
    "Maintain":    0,
    "Bulk":     +300,
}

_GOAL_PROTEIN_FACTOR = {
    "Cut":      2.2,
    "Maintain": 1.8,
    "Bulk":     2.0,
}

_FAT_FRACTION = 0.25   # 25 % of calories from fat
_FAT_KCAL_PER_G = 9
_CARB_KCAL_PER_G = 4
_PROTEIN_KCAL_PER_G = 4


def calculate_macros(profile: dict) -> dict:
    """Return daily macro targets derived from the user profile.

    Uses Mifflin-St Jeor for BMR, an activity multiplier for TDEE, and
    goal-based calorie / protein adjustments.

    Returns:
        Dict with keys: kcal (int), protein_g (int), fat_g (int), carbs_g (int).
        Falls back to a safe default (2000 kcal) if required fields are missing.
    """
    weight = profile.get("weight_kg")
    height = profile.get("height_cm")
    age    = profile.get("age")
    goal   = profile.get("primary_goal", "Maintain")
    level  = profile.get("fitness_level", "Intermediate")
    sex    = profile.get("sex", "male")  # default male; add to profile later if needed

    if not all([weight, height, age]):
        logger.warning("calculate_macros: missing weight/height/age — using defaults")
        return {"kcal": 2000, "protein_g": 150, "fat_g": 56, "carbs_g": 213}

    # Mifflin-St Jeor BMR
    if sex == "female":
        bmr = 10 * weight + 6.25 * height - 5 * age - 161
    else:
        bmr = 10 * weight + 6.25 * height - 5 * age + 5

    activity = _ACTIVITY_MULTIPLIER.get(level, 1.55)
    tdee = bmr * activity
    delta = _GOAL_CALORIE_DELTA.get(goal, 0)
    kcal = round(tdee + delta)

    protein_factor = _GOAL_PROTEIN_FACTOR.get(goal, 1.8)
    protein_g = round(weight * protein_factor)

    fat_g = round((kcal * _FAT_FRACTION) / _FAT_KCAL_PER_G)

    protein_kcal = protein_g * _PROTEIN_KCAL_PER_G
    fat_kcal     = fat_g * _FAT_KCAL_PER_G
    carbs_g      = max(0, round((kcal - protein_kcal - fat_kcal) / _CARB_KCAL_PER_G))

    return {"kcal": kcal, "protein_g": protein_g, "fat_g": fat_g, "carbs_g": carbs_g}


def compute_totals(meals: list[dict]) -> dict:
    """Sum macros across a list of logged meal dicts.

    Returns:
        Dict with keys: kcal, protein_g, fat_g, carbs_g — all int.
    """
    return {
        "kcal":      sum(m.get("kcal", 0)      for m in meals),
        "protein_g": sum(m.get("protein_g", 0) for m in meals),
        "fat_g":     sum(m.get("fat_g", 0)     for m in meals),
        "carbs_g":   sum(m.get("carbs_g", 0)   for m in meals),
    }


def compute_remaining(targets: dict, logged_meals: list[dict]) -> dict:
    """Subtract logged totals from daily targets, flooring each value at 0."""
    totals = compute_totals(logged_meals)
    return {
        "kcal":      max(0, targets["kcal"]      - totals["kcal"]),
        "protein_g": max(0, targets["protein_g"] - totals["protein_g"]),
        "fat_g":     max(0, targets["fat_g"]     - totals["fat_g"]),
        "carbs_g":   max(0, targets["carbs_g"]   - totals["carbs_g"]),
    }


# ---------------------------------------------------------------------------
# Keyboard builders
# ---------------------------------------------------------------------------

_SLOT_EMOJI = {"breakfast": "🌅", "lunch": "☀️", "dinner": "🌙"}
_SLOT_LABEL = {"breakfast": "Breakfast", "lunch": "Lunch", "dinner": "Dinner"}


def _slot_keyboard() -> InlineKeyboardMarkup:
    """Row of breakfast / lunch / dinner buttons."""
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(f"{_SLOT_EMOJI[s]} {_SLOT_LABEL[s]}", callback_data=f"nutr:meal:{s}")
        for s in ("breakfast", "lunch", "dinner")
    ]])


def _meal_options_keyboard(slot: str) -> InlineKeyboardMarkup:
    """Keyboard shown after Claude returns two meal options."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Option 1", callback_data="nutr:log:0"),
            InlineKeyboardButton("✅ Option 2", callback_data="nutr:log:1"),
        ],
        [
            InlineKeyboardButton("🔄 More ideas",        callback_data=f"nutr:more:{slot}"),
            InlineKeyboardButton("🥕 Use my ingredients", callback_data=f"nutr:ingredients:{slot}"),
        ],
    ])


def _after_log_keyboard(logged_slots: set[str]) -> InlineKeyboardMarkup:
    """Keyboard shown after logging a meal — offers remaining unlogged slots + full day view."""
    remaining = [s for s in ("breakfast", "lunch", "dinner") if s not in logged_slots]
    buttons = [
        InlineKeyboardButton(f"{_SLOT_EMOJI[s]} Plan {_SLOT_LABEL[s]}", callback_data=f"nutr:meal:{s}")
        for s in remaining
    ]
    buttons.append(InlineKeyboardButton("📊 See full day", callback_data="nutr:day"))
    # Split into rows of 2
    rows = [buttons[i:i+2] for i in range(0, len(buttons), 2)]
    return InlineKeyboardMarkup(rows)


def _ingredient_meal_keyboard(slot: str) -> InlineKeyboardMarkup:
    """Keyboard shown after Claude generates an ingredient-based meal."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ I'll make this",  callback_data="nutr:log:0"),
            InlineKeyboardButton("🔄 Different idea",  callback_data=f"nutr:more:{slot}"),
        ],
        [
            InlineKeyboardButton("🛒 Shopping list", callback_data=f"nutr:shopping:{slot}"),
        ],
    ])


# ---------------------------------------------------------------------------
# Message formatters
# ---------------------------------------------------------------------------

def _macro_line(m: dict) -> str:
    return f"{m['kcal']} kcal | {m['protein_g']}g protein | {m['fat_g']}g fat | {m['carbs_g']}g carbs"


def format_macro_dashboard(targets: dict, logged_meals: list[dict]) -> tuple[str, InlineKeyboardMarkup]:
    """Build the main nutrition dashboard message and slot keyboard.

    Returns:
        (message_text, keyboard)
    """
    lines = [f"🥗 **Today's nutrition targets:**\nTotal: ~{_macro_line(targets)}"]

    if logged_meals:
        totals    = compute_totals(logged_meals)
        remaining = compute_remaining(targets, logged_meals)
        lines.append(f"\n✅ Logged so far: {_macro_line(totals)}")
        lines.append(f"⏳ Remaining: {_macro_line(remaining)}")

    return "\n".join(lines), _slot_keyboard()


def format_meal_options(options: list[dict], slot: str) -> tuple[str, InlineKeyboardMarkup]:
    """Format two Claude-generated meal options into a Telegram message.

    Args:
        options: List of 2 meal dicts from brain.get_meal_suggestions().
        slot:    "breakfast", "lunch", or "dinner".

    Returns:
        (message_text, keyboard)
    """
    lines = []
    for i, opt in enumerate(options, 1):
        lines.append(
            f"**Option {i} — {opt['name']}** 🕐 {opt['time_min']} min\n"
            f"{opt['kcal']} kcal | {opt['protein_g']}g protein | "
            f"{opt['fat_g']}g fat | {opt['carbs_g']}g carbs\n"
            f"→ Why: {opt['reasoning']}"
        )
    lines.append(
        "\n💡 Have specific ingredients? Tell me what's in your fridge and I'll build a meal around them.\n"
        "Or ask for more ideas anytime."
    )
    return "\n\n".join(lines), _meal_options_keyboard(slot)


def format_ingredient_meal(meal: dict, tip: str, slot: str) -> tuple[str, InlineKeyboardMarkup]:
    """Format a single ingredient-based meal returned by Claude.

    Args:
        meal: Meal dict with keys: name, kcal, protein_g, fat_g, carbs_g, time_min,
              prep_method, reasoning, uses (list of ingredients used).
        tip:  One-line suggestion for what to add to improve the meal.
        slot: "breakfast", "lunch", or "dinner".

    Returns:
        (message_text, keyboard)
    """
    uses = ", ".join(meal.get("uses", []))
    lines = [
        f"🥗 **{meal['name']}**\n"
        f"{meal['kcal']} kcal | {meal['protein_g']}g protein | "
        f"{meal['fat_g']}g fat | {meal['carbs_g']}g carbs\n"
        f"🕐 {meal['time_min']} min | Uses: {uses}\n\n"
        f"Prep: {meal['prep_method']}\n\n"
        f"→ Why: {meal['reasoning']}"
    ]
    if tip:
        lines.append(f"\n💡 {tip}")
    return "\n".join(lines), _ingredient_meal_keyboard(slot)


def format_logged_confirmation(
    meal: dict,
    remaining: dict,
    logged_slots: set[str],
) -> tuple[str, InlineKeyboardMarkup]:
    """Build the confirmation message shown after a meal is logged.

    Returns:
        (message_text, keyboard)
    """
    text = (
        f"✅ Logged! **{meal['name']}** added to your day.\n\n"
        f"Remaining today: {_macro_line(remaining)}"
    )
    return text, _after_log_keyboard(logged_slots)


def format_full_day_view(logged_meals: list[dict], targets: dict) -> tuple[str, InlineKeyboardMarkup]:
    """Build the full-day nutrition summary message.

    Returns:
        (message_text, keyboard)
    """
    by_slot = {m["slot"]: m for m in logged_meals}
    lines = ["📊 **Today's nutrition summary:**\n"]

    for slot in ("breakfast", "lunch", "dinner"):
        emoji = _SLOT_EMOJI[slot]
        label = _SLOT_LABEL[slot]
        if slot in by_slot:
            m = by_slot[slot]
            lines.append(f"{emoji} {label}: {m['name']} — {m['kcal']} kcal | {m['protein_g']}g protein")
        else:
            lines.append(f"{emoji} {label}: not logged yet")

    if logged_meals:
        totals    = compute_totals(logged_meals)
        remaining = compute_remaining(targets, logged_meals)
        lines.append(f"\n✅ Total so far: {_macro_line(totals)}")
        lines.append(f"⏳ Remaining: {_macro_line(remaining)}")

    return "\n".join(lines), _slot_keyboard()


# ---------------------------------------------------------------------------
# Claude prompt builders
# ---------------------------------------------------------------------------

def build_meal_suggestion_prompt(
    slot: str,
    targets: dict,
    remaining: dict,
    profile: dict,
    logged_meals: list[dict],
) -> str:
    """Assemble the user-facing prompt sent to Claude for meal suggestions.

    Args:
        slot:        "breakfast", "lunch", or "dinner".
        targets:     Full daily macro targets.
        remaining:   Macros still available for the rest of the day.
        profile:     User profile dict.
        logged_meals: Meals already logged today (for context).

    Returns:
        Prompt string to pass to brain.get_meal_suggestions().
    """
    goal       = profile.get("primary_goal", "Maintain")
    dietary    = profile.get("dietary_restrictions", "None")
    already    = "; ".join(m["name"] for m in logged_meals) if logged_meals else "none"

    return (
        f"Suggest exactly 2 {slot} options for this user.\n\n"
        f"Daily targets: {_macro_line(targets)}\n"
        f"Remaining macros for today: {_macro_line(remaining)}\n"
        f"Goal: {goal}\n"
        f"Dietary restrictions: {dietary}\n"
        f"Already logged today: {already}\n\n"
        "Requirements:\n"
        "- Option 1 must be quick and simple (under 15 minutes).\n"
        "- Option 2 must be more complete and cooked (different style, not a variation of Option 1).\n"
        "- Fit the remaining macros as closely as possible.\n"
        "- Keep reasoning to one specific sentence per option — no generic advice."
    )


def build_ingredient_meal_prompt(
    ingredients: str,
    slot: str,
    targets: dict,
    remaining: dict,
    profile: dict,
) -> str:
    """Assemble the prompt for an ingredient-based meal request.

    Args:
        ingredients: Raw ingredient list from the user.
        slot:        "breakfast", "lunch", or "dinner".
        targets:     Full daily macro targets.
        remaining:   Macros still available for the rest of the day.
        profile:     User profile dict.

    Returns:
        Prompt string to pass to brain.get_ingredient_meal().
    """
    goal    = profile.get("primary_goal", "Maintain")
    dietary = profile.get("dietary_restrictions", "None")

    return (
        f"Build one {slot} meal using only these ingredients: {ingredients}\n\n"
        f"Daily targets: {_macro_line(targets)}\n"
        f"Remaining macros for today: {_macro_line(remaining)}\n"
        f"Goal: {goal}\n"
        f"Dietary restrictions: {dietary}\n\n"
        "Requirements:\n"
        "- Use only the listed ingredients (pantry staples like oil, salt, spices are allowed).\n"
        "- Include a brief prep method (2–3 sentences max).\n"
        "- Identify 1-3 missing items that would significantly improve the meal's nutrition.\n"
        "- Keep reasoning to one specific sentence — no generic advice."
    )
