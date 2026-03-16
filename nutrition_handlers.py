"""
Telegram handlers for the nutrition feature.

Owns the ingredient-collection ConversationHandler and the nutr: callback dispatcher.
bot.py imports the two public factory/handler functions and registers them.

Architecture:
  - All Claude calls go through brain.py (get_meal_suggestions, get_ingredient_meal).
  - All DynamoDB access goes through storage.py.
  - All message formatting goes through nutrition.py.
  - This module only handles Telegram routing and orchestration.
"""
import logging
from datetime import date, datetime, timezone

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import storage
from auth import is_allowed
from utils import israel_today
from brain import get_ingredient_meal, get_meal_suggestions
from briefing import md_to_html
import nutrition

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Ingredient collection ConversationHandler — states
# ---------------------------------------------------------------------------

_NUTR_INGREDIENTS = 0  # waiting for the user to type their ingredient list


# ---------------------------------------------------------------------------
# Ingredient ConversationHandler — internal handlers
# ---------------------------------------------------------------------------

async def _nutr_ingredient_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point: [🥕 Use my ingredients] button (nutr:ingredients:<slot>).

    Checks for a recently saved grocery list. If one exists from within 3 days,
    shows confirm / update buttons and exits the wizard (those buttons re-enter
    or bypass it as needed). Otherwise asks for a new ingredient list.
    """
    query = update.callback_query
    await query.answer()
    user_id = str(query.from_user.id)
    slot = query.data.split(":")[2]  # "nutr:ingredients:lunch" → "lunch"
    context.user_data["nutr_slot"] = slot

    groceries, updated_at = storage.load_groceries(user_id)
    if groceries and updated_at:
        updated_date = datetime.fromisoformat(updated_at).date()
        days_ago = (date.today() - updated_date).days
        if days_ago <= 3:
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Same ingredients",  callback_data=f"nutr:confirm_groceries:{slot}"),
                InlineKeyboardButton("🔄 Update list",       callback_data=f"nutr:new_groceries:{slot}"),
            ]])
            preview = groceries if len(groceries) <= 120 else groceries[:120] + "…"
            await query.message.reply_text(
                f"Last time you had: {preview}\n\nSame ingredients or anything changed?",
                reply_markup=kb,
            )
            # Exit wizard — the confirm/update buttons are handled separately:
            # confirm_groceries → handle_nutrition_callback
            # new_groceries     → _nutr_new_groceries_entry (second entry point below)
            return ConversationHandler.END

    return await _nutr_ask_ingredients(update, context)


async def _nutr_new_groceries_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point: [🔄 Update list] button (nutr:new_groceries:<slot>).

    The user wants to provide a fresh ingredient list. Stores the slot and
    enters the _NUTR_INGREDIENTS state so the next message is captured.
    """
    query = update.callback_query
    await query.answer()
    slot = query.data.split(":")[2]  # "nutr:new_groceries:lunch" → "lunch"
    context.user_data["nutr_slot"] = slot
    return await _nutr_ask_ingredients(update, context)


async def _nutr_ask_ingredients(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Send the 'what's in your fridge?' prompt and enter the waiting state."""
    await update.effective_message.reply_text(
        "What do you have in your fridge and pantry? List what you've got."
    )
    return _NUTR_INGREDIENTS


async def _nutr_receive_ingredients(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Collect the ingredient list, save it, call Claude, and send the meal."""
    user_id = str(update.effective_user.id)
    ingredients = update.message.text.strip()
    slot = context.user_data.get("nutr_slot", "lunch")

    storage.save_groceries(user_id, ingredients)

    profile = storage.load_profile(user_id)
    today_str = israel_today()
    logged_meals = storage.load_daily_meals(user_id, today_str)
    targets = nutrition.calculate_macros(profile)
    remaining = nutrition.compute_remaining(targets, logged_meals)

    await update.effective_chat.send_action("typing")
    try:
        prompt = nutrition.build_ingredient_meal_prompt(ingredients, slot, targets, remaining, profile)
        meal = get_ingredient_meal(prompt)
    except Exception as exc:
        logger.error("get_ingredient_meal failed for %s: %s", user_id, exc)
        await update.message.reply_text("Had trouble building a meal — try again in a moment.")
        return ConversationHandler.END

    # Cache as single pending option so nutr:log:0 can log it.
    storage.save_pending_meal_options(user_id, slot, [meal])

    text, kb = nutrition.format_ingredient_meal(meal, meal.get("tip", ""), slot)
    await update.message.reply_text(md_to_html(text), parse_mode="HTML", reply_markup=kb)
    return ConversationHandler.END


async def _nutr_ingredient_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle /cancel inside the ingredient collection wizard."""
    context.user_data.pop("nutr_slot", None)
    await update.message.reply_text("Ingredient planning cancelled.")
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------

def build_nutrition_ingredient_handler() -> ConversationHandler:
    """Return the ConversationHandler for ingredient-based meal planning.

    Two entry points:
      nutr:ingredients:<slot>   — first tap, may show confirm/update or ask directly
      nutr:new_groceries:<slot> — user chose to update their ingredient list
    """
    return ConversationHandler(
        entry_points=[
            CallbackQueryHandler(_nutr_ingredient_entry,    pattern=r"^nutr:ingredients:"),
            CallbackQueryHandler(_nutr_new_groceries_entry, pattern=r"^nutr:new_groceries:"),
        ],
        states={
            _NUTR_INGREDIENTS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, _nutr_receive_ingredients),
            ],
        },
        fallbacks=[CommandHandler("cancel", _nutr_ingredient_cancel)],
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

async def _send_meal_options(
    query,
    user_id: str,
    slot: str,
    targets: dict,
    logged_meals: list[dict],
    profile: dict,
) -> None:
    """Generate two meal options via Claude and send them to the user.

    Shared by nutr:meal and nutr:more to avoid duplication.
    """
    remaining = nutrition.compute_remaining(targets, logged_meals)
    await query.message.chat.send_action("typing")
    try:
        prompt = nutrition.build_meal_suggestion_prompt(slot, targets, remaining, profile, logged_meals)
        options = get_meal_suggestions(prompt)
    except Exception as exc:
        logger.error("get_meal_suggestions failed for %s: %s", user_id, exc)
        await query.message.reply_text("Had trouble generating meal ideas — try again in a moment.")
        return
    storage.save_pending_meal_options(user_id, slot, options)
    text, kb = nutrition.format_meal_options(options, slot)
    await query.message.reply_text(md_to_html(text), parse_mode="HTML", reply_markup=kb)


# ---------------------------------------------------------------------------
# Nutrition callback dispatcher (nutr: prefix)
# ---------------------------------------------------------------------------

async def handle_nutrition_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Dispatch all nutr: inline keyboard callbacks for the nutrition flow.

    Registered in bot.py with pattern r"^nutr:". The nutrition ConversationHandler
    (registered first) intercepts nutr:ingredients: and nutr:new_groceries: before
    this handler sees them, so those actions are NOT present here.
    """
    query = update.callback_query
    user_id = str(query.from_user.id)
    if not is_allowed(query.from_user.id):
        await query.answer()
        return

    await query.answer()
    parts = query.data.split(":")  # e.g. ["nutr", "meal", "breakfast"]
    action = parts[1]
    today_str = israel_today()

    profile = storage.load_profile(user_id)
    logged_meals = storage.load_daily_meals(user_id, today_str)
    targets = nutrition.calculate_macros(profile)

    # ── nutr:show — macro dashboard ──────────────────────────────────────────
    if action == "show":
        text, kb = nutrition.format_macro_dashboard(targets, logged_meals)
        await query.message.reply_text(md_to_html(text), parse_mode="HTML", reply_markup=kb)

    # ── nutr:meal:<slot> — generate two meal options ──────────────────────────
    elif action == "meal":
        await _send_meal_options(query, user_id, parts[2], targets, logged_meals, profile)

    # ── nutr:log:<index> — log a pending meal option ──────────────────────────
    elif action == "log":
        index = int(parts[2])
        pending = storage.load_pending_meal_options(user_id)
        if not pending or index >= len(pending["options"]):
            await query.message.reply_text("Couldn't find that meal — try generating options again.")
            return
        if pending.get("date") != today_str:
            storage.clear_pending_meal_options(user_id)
            await query.message.reply_text("Those options are from a previous day — please generate new ones.")
            return
        slot = pending["slot"]
        meal_data = pending["options"][index]
        meal = {
            "name":      meal_data["name"],
            "slot":      slot,
            "kcal":      meal_data["kcal"],
            "protein_g": meal_data["protein_g"],
            "fat_g":     meal_data["fat_g"],
            "carbs_g":   meal_data["carbs_g"],
            "logged_at": datetime.now(timezone.utc).isoformat(),
        }

        storage.log_meal(user_id, today_str, meal)
        logged_meals = [m for m in logged_meals if m.get("slot") != slot] + [meal]
        remaining = nutrition.compute_remaining(targets, logged_meals)
        logged_slots = {m["slot"] for m in logged_meals}
        text, kb = nutrition.format_logged_confirmation(meal, remaining, logged_slots)
        await query.message.reply_text(md_to_html(text), parse_mode="HTML", reply_markup=kb)

    # ── nutr:more:<slot> — regenerate options for the same slot ───────────────
    elif action == "more":
        await _send_meal_options(query, user_id, parts[2], targets, logged_meals, profile)

    # ── nutr:confirm_groceries:<slot> — reuse saved grocery list ─────────────
    elif action == "confirm_groceries":
        slot = parts[2]
        ingredients, _ = storage.load_groceries(user_id)
        if not ingredients:
            await query.message.reply_text("Couldn't find your saved ingredients — please list them again.")
            return
        remaining = nutrition.compute_remaining(targets, logged_meals)
        await query.message.chat.send_action("typing")
        try:
            prompt = nutrition.build_ingredient_meal_prompt(ingredients, slot, targets, remaining, profile)
            meal_data = get_ingredient_meal(prompt)
        except Exception as exc:
            logger.error("get_ingredient_meal failed for %s: %s", user_id, exc)
            await query.message.reply_text("Had trouble building a meal — try again in a moment.")
            return
        storage.save_pending_meal_options(user_id, slot, [meal_data])
        text, kb = nutrition.format_ingredient_meal(meal_data, meal_data.get("tip", ""), slot)
        await query.message.reply_text(md_to_html(text), parse_mode="HTML", reply_markup=kb)

    # ── nutr:day — full day summary ───────────────────────────────────────────
    elif action == "day":
        text, kb = nutrition.format_full_day_view(logged_meals, targets)
        await query.message.reply_text(md_to_html(text), parse_mode="HTML", reply_markup=kb)

    # ── nutr:shopping:<slot> — placeholder ────────────────────────────────────
    elif action == "shopping":
        await query.message.reply_text("🛒 Shopping list coming soon!")


async def handle_nutrition_briefing_tap(query, user_id: str) -> None:
    """Handle the 🥗 Nutrition button tap from the morning briefing.

    Called from bot.py's handle_briefing_action when action == "nutrition".
    Shows the macro dashboard for today.
    """
    today_str = israel_today()
    profile = storage.load_profile(user_id)
    logged_meals = storage.load_daily_meals(user_id, today_str)
    targets = nutrition.calculate_macros(profile)
    text, kb = nutrition.format_macro_dashboard(targets, logged_meals)
    await query.message.reply_text(md_to_html(text), parse_mode="HTML", reply_markup=kb)
