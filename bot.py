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

import garmin_daily_stats
import storage
from auth import is_allowed
from brain import get_claude_response, extract_memorable_facts
from briefing import fetch_weather, md_to_html, send_morning_briefing
from profile_wizard import build_wizard_handler

load_dotenv()

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
        garmin_daily_stats.initial_login(str(user_id), email, password)
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
            "Garmin detects you've woken up (but no later than 10:00 AM)."
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

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

    history = storage.load_history(str(user_id))
    profile = storage.load_profile(str(user_id))
    weather = fetch_weather()
    garmin_data = garmin_daily_stats.fetch_daily_stats(str(user_id))

    try:
        reply = get_claude_response(history, user_text, weather, garmin_data, profile)
    except Exception as exc:
        logger.error("Claude error: %s", exc)
        await update.message.reply_text(
            "Sorry, I had trouble thinking just now. Please try again in a moment."
        )
        return

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    history.append({"role": "user", "content": user_text, "ts": today})
    history.append({"role": "assistant", "content": reply, "ts": today})

    # Drop messages older than 7 days (with fact extraction on what's dropped).
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")
    stale = [m for m in history if m.get("ts", today) < cutoff]
    if stale:
        history = [m for m in history if m.get("ts", today) >= cutoff]
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
            full_text = cached["full_recommendation"]
        else:
            await query.message.reply_text("No workout cached for today — send /morning to generate one.")
            return
        await query.message.reply_text(md_to_html(full_text), parse_mode="HTML")

    elif action == "nutrition":
        await query.message.reply_text("🥗 Nutrition planning coming soon!")

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
    app.add_handler(CommandHandler("clear", clear))
    app.add_handler(CommandHandler("remember", remember))
    app.add_handler(CommandHandler("morning", morning))
    app.add_handler(CommandHandler("settime", settime))
    app.add_handler(CallbackQueryHandler(handle_briefing_action, pattern=r"^action:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Bot starting in polling mode…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
