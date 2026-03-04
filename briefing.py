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
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup

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

_RECOVERY_LABEL = {
    "high":     "Recovery looks solid today",
    "moderate": "Recovery is decent today",
    "low":      "Recovery is a bit low today",
    "very_low": "Recovery is very low today",
}

_BRIEFING_KEYBOARD = InlineKeyboardMarkup([
    [
        InlineKeyboardButton("💪 Workout",   callback_data="action:workout"),
        InlineKeyboardButton("🥗 Nutrition", callback_data="action:nutrition"),
    ],
    [
        InlineKeyboardButton("😴 Sleep",     callback_data="action:sleep"),
        InlineKeyboardButton("💧 Hydration", callback_data="action:hydration"),
    ],
])

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


def _build_recovery_line(garmin_data: dict, tier: str) -> str:
    """Build the one-line recovery summary shown in the briefing header."""
    sleep = garmin_data.get("sleep", {})
    hrv = garmin_data.get("hrv", {})
    sleep_score = sleep.get("sleep_score")
    hrv_last = hrv.get("last_night_avg")
    hrv_avg = hrv.get("weekly_avg")

    label = _RECOVERY_LABEL.get(tier, "Recovery status unknown")
    parts = []
    if sleep_score is not None:
        parts.append(f"sleep at {sleep_score}/100")
    if hrv_last is not None and hrv_avg is not None:
        direction = "above" if hrv_last >= hrv_avg else "below"
        parts.append(f"HRV {direction} baseline")

    detail = " — " + ", ".join(parts) if parts else ""
    return f"{label}{detail}."


async def send_morning_briefing(
    bot: Bot,
    chat_id: int,
    user_id_str: str,
    profile_fallback_path: str | None = None,
) -> None:
    """Generate the full workout recommendation, cache it, and send the morning briefing.

    The briefing message shows a short recovery line, workout summary, and motivational
    sentence — with inline buttons for each topic. The full workout detail is cached in
    DynamoDB so the Workout button can serve it instantly without a second Claude call.

    Marks today as sent in storage on success.
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
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

    storage.save_daily_workout(user_id_str, result, today)

    tier = result["recovery_tier"]
    emoji = _RECOVERY_EMOJI.get(tier, "⚪")
    recovery_line = _build_recovery_line(garmin_data, tier)

    briefing_text = (
        f"{emoji} {recovery_line}\n"
        f"{result['summary']}\n"
        f"{result['motivation']}"
    )

    await bot.send_message(
        chat_id,
        md_to_html(briefing_text),
        parse_mode="HTML",
        reply_markup=_BRIEFING_KEYBOARD,
    )

    history.append({"role": "user", "content": "/morning", "ts": today})
    history.append({"role": "assistant", "content": briefing_text, "ts": today})
    storage.save_history(user_id_str, history)

    storage.mark_morning_sent(user_id_str, today)
    logger.info("Morning briefing sent and marked for %s.", today)
