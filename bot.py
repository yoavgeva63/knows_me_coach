"""
Local development entry point for the Telegram fitness coach bot.
Run this file directly to test the bot locally with long-polling.

For production, the Lambda handler (lambda_handler.py) handles webhook updates instead.

Usage:
    python bot.py
"""
import json
import logging
import os
import time
from collections import defaultdict
from pathlib import Path

import requests
from dotenv import load_dotenv
from garminconnect import GarminConnectAuthenticationError
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

import garmin
from brain import get_claude_response
from workout_recommender import get_workout_recommendation

_RECOVERY_EMOJI = {
    "high":      "🟢",
    "moderate":  "🟡",
    "low":       "🟠",
    "very_low":  "🔴",
}

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Load the user profile once at startup so /morning doesn't hit disk every time.
_profile_path = Path(__file__).parent / "user_profile.json"
USER_PROFILE: dict = json.loads(_profile_path.read_text(encoding="utf-8")) if _profile_path.exists() else {}

# In-memory conversation history keyed by Telegram user ID.
# In production this will be replaced by DynamoDB.
conversation_histories: dict[int, list[dict]] = defaultdict(list)

_raw_id = os.environ.get("ALLOWED_TELEGRAM_USER_ID", "")
ALLOWED_USER_ID = int(_raw_id) if _raw_id.lstrip("-").isdigit() else 0


_weather_cache: dict = {"value": "", "expires": 0.0}
_WEATHER_TTL = 14400  # seconds — refresh at most every 4 hours


def fetch_weather() -> str:
    """Return a one-line weather string for Tel Aviv using Open-Meteo (no API key needed).

    Result is cached for 30 minutes so every chat message doesn't hit the API.
    """
    if time.monotonic() < _weather_cache["expires"]:
        return _weather_cache["value"]

    # WMO weather interpretation codes → human-readable descriptions
    WMO = {
        0: "Clear sky", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
        45: "Foggy", 48: "Icy fog",
        51: "Light drizzle", 53: "Drizzle", 55: "Heavy drizzle",
        61: "Light rain", 63: "Rain", 65: "Heavy rain",
        71: "Light snow", 73: "Snow", 75: "Heavy snow",
        80: "Rain showers", 81: "Showers", 82: "Heavy showers",
        95: "Thunderstorm",
    }
    try:
        url = (
            "https://api.open-meteo.com/v1/forecast"
            "?latitude=32.0853&longitude=34.7818"
            "&current=temperature_2m,apparent_temperature,weather_code,wind_speed_10m"
            "&daily=temperature_2m_min,temperature_2m_max"
            "&timezone=Asia/Jerusalem"
        )
        resp = requests.get(url, timeout=8)
        resp.raise_for_status()
        data = resp.json()
        current = data["current"]
        temp = round(current["temperature_2m"])
        feels = round(current["apparent_temperature"])
        wind = round(current["wind_speed_10m"])
        desc = WMO.get(current["weather_code"], "Unknown")
        t_min = round(data["daily"]["temperature_2m_min"][0])
        t_max = round(data["daily"]["temperature_2m_max"][0])
        result = f"Tel Aviv: {desc}, now {temp}°C (feels like {feels}°C), {t_min}–{t_max}°C today, wind {wind} km/h"
    except Exception as exc:
        logger.warning("Weather fetch failed: %s", exc)
        result = "Weather unavailable"

    _weather_cache["value"] = result
    _weather_cache["expires"] = time.monotonic() + _WEATHER_TTL
    return result


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

    conversation_histories[user_id].clear()
    await update.message.reply_text(
        "Hey! I'm your personal fitness coach 💪\n\n"
        "Tell me about your fitness goals, ask for a workout plan, log a meal, "
        "or just chat about your health. I'm here to help!\n\n"
        "Use /clear to reset our conversation anytime."
    )


async def clear(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /clear command — wipes conversation history."""
    user_id = update.effective_user.id
    if not is_allowed(user_id):
        return

    conversation_histories[user_id].clear()
    await update.message.reply_text("Conversation cleared. Fresh start! 🔄")


async def morning(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /morning command — fetch Garmin data and send a personalised briefing."""
    user_id = update.effective_user.id
    if not is_allowed(user_id):
        return

    await context.bot.send_chat_action(
        chat_id=update.effective_chat.id, action="typing"
    )

    # Fetch weather (non-blocking fallback on error)
    weather = fetch_weather()

    # Fetch Garmin data
    try:
        garmin_data = garmin.fetch_daily_stats()
    except GarminConnectAuthenticationError:
        await update.message.reply_text(
            "Couldn't connect to Garmin — check your credentials in .env."
        )
        return
    except ValueError as exc:
        await update.message.reply_text(f"Garmin config error: {exc}")
        return
    except Exception as exc:
        logger.error("Garmin fetch error: %s", exc)
        await update.message.reply_text(
            "Had trouble fetching your Garmin data. Please try again in a moment."
        )
        return

    # Generate workout recommendation via rules + Claude
    try:
        result = get_workout_recommendation(garmin_data, USER_PROFILE, weather)
    except Exception as exc:
        logger.error("Workout recommendation error: %s", exc)
        await update.message.reply_text(
            "Had trouble generating your recommendation. Please try again in a moment."
        )
        return

    emoji = _RECOVERY_EMOJI.get(result["recovery_tier"], "⚪")
    await update.message.reply_text(f"{emoji} {result['recommendation']}")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle all incoming text messages."""
    user_id = update.effective_user.id
    if not is_allowed(user_id):
        logger.warning("Blocked message from unauthorized user %s", user_id)
        return

    user_text = update.message.text
    logger.info("Message from %s: %s", user_id, user_text[:80])

    # Show typing indicator while waiting for Claude
    await context.bot.send_chat_action(
        chat_id=update.effective_chat.id, action="typing"
    )

    history = conversation_histories[user_id]
    weather = fetch_weather()

    try:
        reply = get_claude_response(history, user_text, weather)
    except Exception as exc:
        logger.error("Claude error: %s", exc)
        await update.message.reply_text(
            "Sorry, I had trouble thinking just now. Please try again in a moment."
        )
        return

    # Persist the exchange in memory
    history.append({"role": "user", "content": user_text})
    history.append({"role": "assistant", "content": reply})

    # Keep history bounded to last 40 messages (20 exchanges) to control token cost
    if len(history) > 40:
        conversation_histories[user_id] = history[-40:]

    await update.message.reply_text(reply)


def main() -> None:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("clear", clear))
    app.add_handler(CommandHandler("morning", morning))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Bot starting in polling mode…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
