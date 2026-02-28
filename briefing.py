"""
Shared morning briefing logic.
Used by bot.py (/morning command), morning_check.py (cron), and lambda_handler.py.
"""
import logging
import re
import time
from datetime import datetime, timezone

import requests
from garminconnect import GarminConnectAuthenticationError
from telegram import Bot

import garmin_daily_stats
import storage
from workout_recommender import get_workout_recommendation

logger = logging.getLogger(__name__)

_RECOVERY_EMOJI = {
    "high":     "🟢",
    "moderate": "🟡",
    "low":      "🟠",
    "very_low": "🔴",
}

_weather_cache: dict = {"value": "", "expires": 0.0}
_WEATHER_TTL = 14400  # 4 hours


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


def md_to_html(text: str) -> str:
    """Convert Claude's Markdown to Telegram HTML (bold only)."""
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text, flags=re.DOTALL)
    return text


async def send_morning_briefing(
    bot: Bot,
    chat_id: int,
    user_id_str: str,
    profile_fallback_path: str | None = None,
) -> None:
    """Fetch Garmin data, build the morning briefing, send it, and persist history.

    Marks today as sent in storage on success. Callers do not need to do this.
    """
    weather = fetch_weather()
    profile = storage.load_profile(user_id_str, fallback_path=profile_fallback_path)
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
    await bot.send_message(chat_id, md_to_html(briefing), parse_mode="HTML")

    history.append({"role": "user", "content": "/morning"})
    history.append({"role": "assistant", "content": briefing})
    storage.save_history(user_id_str, history)

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    storage.mark_morning_sent(user_id_str, today)
    logger.info("Morning briefing sent and marked for %s.", today)
