"""
Local development entry point for the Telegram fitness coach bot.
Run this file directly to test the bot locally with long-polling.

For production, the Lambda handler (lambda_handler.py) handles webhook updates instead.
"""
import logging
import os
import re
from datetime import datetime, timezone, timedelta

from dotenv import load_dotenv
from garminconnect import GarminConnectAuthenticationError
from telegram import Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import garmin
import storage
from auth import is_allowed
from brain import get_claude_response, extract_memorable_facts
from briefing import fetch_weather, md_to_html, send_morning_briefing
from nutrition_handlers import (
    build_nutrition_ingredient_handler,
    handle_nutrition_callback,
    handle_nutrition_briefing_tap,
)
from profile_wizard import build_wizard_handler
from workout_recommender import get_workout_recommendation

load_dotenv()

# Keywords that signal a nutrition-related message. When matched, today's logged
# meals are fetched from the already-loaded profile and passed to the coach.
_NUTRITION_KEYWORDS = frozenset({
    "eat", "eating", "ate", "eaten", "food", "meal", "meals", "lunch", "dinner",
    "breakfast", "snack", "calories", "calorie", "protein", "carb", "carbs", "fat",
    "diet", "nutrition", "hungry", "hunger", "fridge", "cook", "recipe", "macro",
    "macros", "kcal", "ingredient", "ingredients",
})


def _is_nutrition_message(text: str) -> bool:
    """Return True if the message likely relates to food or nutrition."""
    words = re.findall(r"[a-z]+", text.lower())
    return bool(_NUTRITION_KEYWORDS.intersection(words))


logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# /connect_garmin mini wizard — 2 states
# ---------------------------------------------------------------------------

_GARMIN_EMAIL, _GARMIN_PASSWORD = range(2)


async def _connect_garmin_start(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle /connect_garmin — start the Garmin credentials wizard."""
    if not is_allowed(update.effective_user.id):
        return ConversationHandler.END
    await update.message.reply_text("What's your Garmin Connect email address?")
    return _GARMIN_EMAIL


async def _connect_garmin_email(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Collect the Garmin email and ask for the password."""
    context.user_data["garmin_email"] = update.message.text.strip()
    await update.message.reply_text(
        "Now enter your Garmin Connect password.\n"
        "⚠️ Your message will be visible in chat — delete it after sending."
    )
    return _GARMIN_PASSWORD


async def _connect_garmin_password(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Collect the password, authenticate, and persist the tokens."""
    user_id = update.effective_user.id
    email = context.user_data.pop("garmin_email", "")
    password = update.message.text.strip()
    context.user_data.clear()

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    try:
        garmin.initial_login(str(user_id), email, password)
    except GarminConnectAuthenticationError:
        await update.message.reply_text(
            "Authentication failed — double-check your email and password and try /connect_garmin again."
        )
        return ConversationHandler.END
    except Exception as exc:
        logger.error("Garmin connect error for %s: %s", user_id, exc)
        await update.message.reply_text("Something went wrong connecting to Garmin. Try again later.")
        return ConversationHandler.END

    await update.message.reply_text(
        "Garmin Connect linked! Your morning briefings will now include live health data."
    )
    return ConversationHandler.END


async def _connect_garmin_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle /cancel inside the connect_garmin wizard."""
    context.user_data.clear()
    await update.message.reply_text("Garmin connection cancelled.")
    return ConversationHandler.END


def _build_garmin_connect_handler() -> ConversationHandler:
    """Return the ConversationHandler for the /connect_garmin mini wizard."""
    return ConversationHandler(
        entry_points=[CommandHandler("connect_garmin", _connect_garmin_start)],
        states={
            _GARMIN_EMAIL: [MessageHandler(filters.TEXT & ~filters.COMMAND, _connect_garmin_email)],
            _GARMIN_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, _connect_garmin_password)],
        },
        fallbacks=[CommandHandler("cancel", _connect_garmin_cancel)],
    )


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

async def clear(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /clear command — wipes conversation history."""
    user_id = update.effective_user.id
    if not is_allowed(user_id):
        return

    storage.clear_history(str(user_id))
    await update.message.reply_text("Conversation cleared. Fresh start! 🔄")


async def remember(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /remember <text> — store a long-term coach note."""
    user_id = update.effective_user.id
    if not is_allowed(user_id):
        return

    note_text = " ".join(context.args).strip()
    if not note_text:
        await update.message.reply_text("Usage: /remember <fact about you>\nExample: /remember I hurt my left knee")
        return

    storage.add_coach_note(str(user_id), note_text)
    await update.message.reply_text("Got it, I'll remember that 🧠")


async def settime(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /settime <HH:MM|sleep> — set the automatic morning alarm."""
    user_id = update.effective_user.id
    if not is_allowed(user_id):
        return

    arg = " ".join(context.args).strip().lower()
    if arg == "sleep":
        storage.set_morning_alarm(str(user_id), "sleep")
        await update.message.reply_text(
            "Got it! I'll send your morning briefing automatically as soon as "
            "Garmin detects you've woken up (but no later than 09:30 AM)."
        )
    elif re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", arg):
        storage.set_morning_alarm(str(user_id), arg)
        await update.message.reply_text(f"Morning alarm set to {arg} (Israel time).")
    else:
        await update.message.reply_text(
            "Usage:\n"
            "  /settime 07:30  — set a fixed time (Israel time)\n"
            "  /settime sleep  — trigger when Garmin detects you've woken up"
        )


async def morning(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /morning command — fetch Garmin data and send a personalised briefing."""
    user_id = update.effective_user.id
    if not is_allowed(user_id):
        return

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    await send_morning_briefing(context.bot, update.effective_chat.id, str(user_id))


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle all incoming text messages."""
    user_id = update.effective_user.id
    if not is_allowed(user_id):
        logger.warning("Blocked message from unauthorized user %s", user_id)
        return

    user_text = update.message.text
    logger.info("Message from %s: %s", user_id, user_text[:80])

    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    profile, daily_workout = storage.load_user_data(str(user_id), today_str)
    if not profile:
        await update.message.reply_text("Welcome! Please run /start to set up your profile first.")
        return

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

    history = storage.load_history(str(user_id))
    weather = fetch_weather()
    garmin_data = garmin.fetch_daily_stats(str(user_id))

    logged_meals: list[dict] | None = None
    if _is_nutrition_message(user_text):
        logged_meals = storage.get_meals_from_profile(profile, today_str)

    try:
        reply, tool_calls = get_claude_response(
            history, user_text, weather, garmin_data, profile, daily_workout, logged_meals
        )
    except Exception as exc:
        logger.error("Claude error: %s", exc)
        await update.message.reply_text(
            "Sorry, I had trouble thinking just now. Please try again in a moment."
        )
        return

    briefing_triggered = False
    for call in tool_calls:
        name = call["name"]
        inp = call["input"]
        try:
            if name == "set_morning_alarm":
                storage.set_morning_alarm(str(user_id), inp["time"])
                logger.info("Tool: set_morning_alarm(%s) for user %s", inp["time"], user_id)
            elif name == "remember_fact":
                storage.add_coach_note(str(user_id), inp["fact"])
                logger.info("Tool: remember_fact for user %s: %s", user_id, inp["fact"])
            elif name == "trigger_morning_briefing":
                # Guard: skip if briefing already sent today (daily_workout date matches today).
                if daily_workout and daily_workout.get("date") == today_str:
                    logger.info("Tool: trigger_morning_briefing skipped — already sent today for %s", user_id)
                else:
                    await send_morning_briefing(context.bot, update.effective_chat.id, str(user_id))
                    briefing_triggered = True
                    logger.info("Tool: trigger_morning_briefing for user %s", user_id)
            elif name == "update_daily_workout":
                if not storage.load_daily_workout(str(user_id), today_str):
                    base = get_workout_recommendation(garmin_data, profile, weather, history)
                    storage.save_daily_workout(str(user_id), base, today_str)
                fields = {"workout_recommendation": inp["workout_recommendation"]}
                if inp.get("summary"):
                    fields["summary"] = inp["summary"]
                storage.patch_daily_workout(str(user_id), fields, today_str)
                logger.info("Tool: update_daily_workout for user %s", user_id)
        except Exception as exc:
            logger.error("Tool execution failed (%s) for user %s: %s", name, user_id, exc)

    # If send_morning_briefing ran, it saved its own history entries — reload to avoid overwriting them.
    if briefing_triggered:
        history = storage.load_history(str(user_id))

    history.append({"role": "user", "content": user_text, "ts": today_str})
    history.append({"role": "assistant", "content": reply, "ts": today_str})

    # Drop messages older than 7 days (with fact extraction on what's dropped).
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")
    stale = [m for m in history if m.get("ts", today_str) < cutoff]
    if stale:
        history = [m for m in history if m.get("ts", today_str) >= cutoff]
        try:
            extracted = extract_memorable_facts(stale, profile.get("coach_notes", []))
            for fact in extracted:
                storage.add_coach_note(str(user_id), fact)
                logger.info("Auto-saved coach note (age-pruned) for %s: %s", user_id, fact)
        except Exception as exc:
            logger.warning("Fact extraction (age-pruned) failed for %s: %s", user_id, exc)

    # Fallback: if still over 40 (very active week), extract from oldest 10.
    if len(history) >= 40:
        messages_to_drop = history[:10]
        try:
            extracted = extract_memorable_facts(messages_to_drop, profile.get("coach_notes", []))
            for fact in extracted:
                storage.add_coach_note(str(user_id), fact)
                logger.info("Auto-saved coach note (count-pruned) for %s: %s", user_id, fact)
        except Exception as exc:
            logger.warning("Fact extraction (count-pruned) failed for %s: %s", user_id, exc)
        history = history[10:]

    storage.save_history(str(user_id), history)

    await update.message.reply_text(md_to_html(reply), parse_mode="HTML")


async def handle_briefing_action(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle inline keyboard button taps from the morning briefing."""
    query = update.callback_query
    user_id = query.from_user.id
    if not is_allowed(user_id):
        await query.answer()
        return

    await query.answer()
    action = query.data.split(":")[1] if ":" in query.data else query.data

    if action == "workout":
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        cached = storage.load_daily_workout(str(user_id), today)
        if cached:
            full_text = cached["workout_recommendation"]
        else:
            await query.message.reply_text("No workout cached for today — send /morning to generate one.")
            return
        await query.message.reply_text(md_to_html(full_text), parse_mode="HTML")

    elif action == "nutrition":
        await handle_nutrition_briefing_tap(query, str(query.from_user.id))

    elif action == "sleep":
        await query.message.reply_text("😴 Sleep insights coming soon!")

    elif action == "hydration":
        await query.message.reply_text("💧 Hydration tracking coming soon!")


# ---------------------------------------------------------------------------
# App entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """Build and start the bot."""
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    app = Application.builder().token(token).build()
    
    storage.ensure_tables()

    app.add_handler(build_wizard_handler())
    app.add_handler(_build_garmin_connect_handler())
    app.add_handler(build_nutrition_ingredient_handler())
    app.add_handler(CommandHandler("clear", clear))
    app.add_handler(CommandHandler("remember", remember))
    app.add_handler(CommandHandler("morning", morning))
    app.add_handler(CommandHandler("settime", settime))
    app.add_handler(CallbackQueryHandler(handle_briefing_action, pattern=r"^action:"))
    app.add_handler(CallbackQueryHandler(handle_nutrition_callback, pattern=r"^nutr:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Bot starting in polling mode…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
