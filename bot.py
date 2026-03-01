"""
Local development entry point for the Telegram fitness coach bot.
Run this file directly to test the bot locally with long-polling.

For production, the Lambda handler (lambda_handler.py) handles webhook updates instead.
"""
import logging
import os
import re

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

import garmin_daily_stats
import storage
from brain import get_claude_response, extract_memorable_facts
from briefing import fetch_weather, md_to_html, send_morning_briefing

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

_PROFILE_PATH = os.path.join(os.path.dirname(__file__), "user_profile.json")

_raw_id = os.environ.get("ALLOWED_TELEGRAM_USER_ID", "")
ALLOWED_USER_ID = int(_raw_id) if _raw_id.lstrip("-").isdigit() else 0


def is_allowed(user_id: int) -> bool:
    """Only respond to the configured user (yourself)."""
    if ALLOWED_USER_ID == 0:
        return True  # No restriction configured — allow all (dev mode)
    return user_id == ALLOWED_USER_ID


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start command."""
    user_id = update.effective_user.id
    if not is_allowed(user_id):
        return

    storage.clear_history(str(user_id))
    await update.message.reply_text(
        "Hey! I'm your personal fitness coach 💪\n\n"
        "Tell me about your fitness goals, ask for a workout plan, log a meal, "
        "or just chat about your health. I'm here to help!\n\n"
        "Use /clear to reset our conversation anytime.\n"
        "Use /remember <fact> to store something I should always know about you."
    )


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
    await send_morning_briefing(
        context.bot,
        update.effective_chat.id,
        str(user_id),
        profile_fallback_path=_PROFILE_PATH,
    )


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
    profile = storage.load_profile(str(user_id), fallback_path=_PROFILE_PATH)
    weather = fetch_weather()
    garmin_data = garmin_daily_stats.fetch_daily_stats()

    try:
        reply = get_claude_response(history, user_text, weather, garmin_data, profile)
    except Exception as exc:
        logger.error("Claude error: %s", exc)
        await update.message.reply_text(
            "Sorry, I had trouble thinking just now. Please try again in a moment."
        )
        return

    history.append({"role": "user", "content": user_text})
    history.append({"role": "assistant", "content": reply})

    # When history hits 40 messages, extract memorable facts from the 10 oldest
    # before they are trimmed, then keep only the 30 most recent.
    if len(history) >= 40:
        messages_to_drop = history[:10]
        try:
            extracted = extract_memorable_facts(messages_to_drop, profile.get("coach_notes", []))
            for fact in extracted:
                storage.add_coach_note(str(user_id), fact)
                logger.info("Auto-saved coach note for %s: %s", user_id, fact)
        except Exception as exc:
            logger.warning("Fact extraction failed for %s: %s", user_id, exc)
        history = history[10:]

    storage.save_history(str(user_id), history)

    await update.message.reply_text(md_to_html(reply), parse_mode="HTML")


def main() -> None:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    app = Application.builder().token(token).build()

    storage.ensure_tables()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("clear", clear))
    app.add_handler(CommandHandler("remember", remember))
    app.add_handler(CommandHandler("morning", morning))
    app.add_handler(CommandHandler("settime", settime))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Bot starting in polling mode…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
