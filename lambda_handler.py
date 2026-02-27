"""
AWS Lambda entry point for the Telegram fitness coach bot.

API Gateway receives Telegram webhook POST → triggers this Lambda.
Credentials are loaded from Lambda environment variables (or set via AWS Secrets Manager
by wrapping them in your Lambda function configuration).

Deploy steps (high level):
1. pip install -r requirements.txt -t package/
2. Zip package/ + *.py + user_profile.json
3. Upload to Lambda, set handler to lambda_handler.lambda_handler
4. Set API Gateway POST route → Lambda
5. Register webhook: https://api.telegram.org/bot<TOKEN>/setWebhook?url=<API_GW_URL>
"""
import json
import logging
import os
import re
import time

import requests
from garminconnect import GarminConnectAuthenticationError
from telegram import Update, Bot

import garmin_daily_stats
import storage
from brain import get_claude_response
from workout_recommender import get_workout_recommendation

logger = logging.getLogger()
logger.setLevel(logging.INFO)

ALLOWED_USER_ID = int(os.environ.get("ALLOWED_TELEGRAM_USER_ID", 0))

_PROFILE_PATH = os.path.join(os.path.dirname(__file__), "user_profile.json")

_RECOVERY_EMOJI = {
    "high":      "🟢",
    "moderate":  "🟡",
    "low":       "🟠",
    "very_low":  "🔴",
}

# Lambda containers can be reused — cache weather and Garmin data in-process
# to avoid redundant API calls within a warm invocation window.
_weather_cache: dict = {"value": "", "expires": 0.0}
_WEATHER_TTL = 14400

def is_allowed(user_id: int) -> bool:
    if ALLOWED_USER_ID == 0:
        return True
    return user_id == ALLOWED_USER_ID


def fetch_weather() -> str:
    """Return a one-line weather string for Tel Aviv using Open-Meteo."""
    if time.monotonic() < _weather_cache["expires"]:
        return _weather_cache["value"]

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


def _md_to_html(text: str) -> str:
    """Convert Claude's Markdown to Telegram HTML (bold only)."""
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text, flags=re.DOTALL)
    return text


async def _process_update(update: Update, bot: Bot) -> None:
    """Core update processing logic — runs inside the Lambda invocation."""
    if not update.message or not update.message.text:
        return

    user_id = update.effective_user.id
    if not is_allowed(user_id):
        logger.warning("Blocked unauthorized user %s", user_id)
        return

    text = update.message.text.strip()
    chat_id = update.effective_chat.id
    user_id_str = str(user_id)

    if text == "/start":
        storage.clear_history(user_id_str)
        await bot.send_message(
            chat_id,
            "Hey! I'm your personal fitness coach 💪\n\n"
            "Tell me about your goals, ask for a workout, log a meal, or just chat!\n\n"
            "Use /clear to reset our conversation anytime.\n"
            "Use /remember <fact> to store something I should always know about you.",
        )
        return

    if text == "/clear":
        storage.clear_history(user_id_str)
        await bot.send_message(chat_id, "Conversation cleared. Fresh start! 🔄")
        return

    if text.startswith("/remember"):
        note_text = text[len("/remember"):].strip()
        if not note_text:
            await bot.send_message(chat_id, "Usage: /remember <fact about you>")
            return
        storage.add_coach_note(user_id_str, note_text)
        await bot.send_message(chat_id, "Got it, I'll remember that. 🧠")
        return

    if text == "/morning":
        weather = fetch_weather()
        profile = storage.load_profile(user_id_str, fallback_path=_PROFILE_PATH)
        history = storage.load_history(user_id_str)
        try:
            garmin_data = garmin_daily_stats.fetch_daily_stats(force_refresh=True)
        except GarminConnectAuthenticationError:
            await bot.send_message(chat_id, "Couldn't connect to Garmin — check your credentials.")
            return
        except Exception as exc:
            logger.error("Garmin fetch error: %s", exc)
            await bot.send_message(chat_id, "Had trouble fetching your Garmin data. Try again in a moment.")
            return
        try:
            result = get_workout_recommendation(garmin_data, profile, weather, history)
        except Exception as exc:
            logger.error("Workout recommendation error: %s", exc)
            await bot.send_message(chat_id, "Had trouble generating your recommendation. Try again in a moment.")
            return
        emoji = _RECOVERY_EMOJI.get(result["recovery_tier"], "⚪")
        briefing = f"{emoji} {result['recommendation']}"
        await bot.send_message(chat_id, _md_to_html(briefing), parse_mode="HTML")

        # Append the morning exchange to history and persist (reuse already-loaded list)
        history.append({"role": "user", "content": "/morning"})
        history.append({"role": "assistant", "content": briefing})
        storage.save_history(user_id_str, history)
        return

    # Regular message
    history = storage.load_history(user_id_str)
    profile = storage.load_profile(user_id_str, fallback_path=_PROFILE_PATH)
    weather = fetch_weather()
    garmin_data = garmin_daily_stats.fetch_daily_stats()

    try:
        reply = get_claude_response(history, text, weather, garmin_data, profile)
    except Exception as exc:
        logger.error("Claude error: %s", exc)
        await bot.send_message(chat_id, "Sorry, I had a hiccup. Try again in a moment.")
        return

    history.append({"role": "user", "content": text})
    history.append({"role": "assistant", "content": reply})
    storage.save_history(user_id_str, history)

    await bot.send_message(chat_id, _md_to_html(reply), parse_mode="HTML")


def lambda_handler(event: dict, context) -> dict:
    """AWS Lambda entry point."""
    import asyncio

    logger.info("Received event: %s", json.dumps(event)[:500])

    # Ensure DynamoDB tables exist (no-op after first invocation on a warm container)
    storage.ensure_tables()

    token = os.environ["TELEGRAM_BOT_TOKEN"]
    bot = Bot(token=token)

    try:
        body = json.loads(event.get("body", "{}"))
        update = Update.de_json(body, bot)
        asyncio.get_event_loop().run_until_complete(_process_update(update, bot))
    except Exception as exc:
        logger.error("Unhandled error: %s", exc, exc_info=True)

    # Always return 200 so Telegram doesn't retry
    return {"statusCode": 200, "body": "ok"}
