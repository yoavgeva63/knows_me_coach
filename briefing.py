"""
Shared morning briefing logic.
Used by bot.py (/morning command), morning_check.py (cron), and lambda_handler.py.
"""
import logging
import re
import time
from datetime import datetime, timezone

from datetime import date, timedelta

import requests
from garminconnect import GarminConnectAuthenticationError
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup

import garmin
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
    """Convert Claude's Markdown to Telegram HTML (bold only).

    Handles both **double** and *single* asterisk bold markers.
    Double asterisks are converted first to avoid double-processing.
    """
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text, flags=re.DOTALL)
    text = re.sub(r'\*([^*\n]+?)\*', r'<b>\1</b>', text)
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
) -> None:
    """Generate the full workout recommendation, cache it, and send the morning briefing.

    The briefing message shows a short recovery line (Garmin users only), workout summary,
    and motivational sentence — with inline buttons for each topic. The full workout detail
    is cached in DynamoDB so the Workout button can serve it instantly without a second call.

    Non-Garmin users skip the recovery line entirely; the workout decision is based on
    the saved workout_history rolling log.

    Marks today as sent in storage on success.
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    weather = fetch_weather()
    profile = storage.load_profile(user_id_str)
    history = storage.load_history(user_id_str)

    # Pass yesterday's cached workout so Claude knows which muscle groups were trained.
    cached = profile.get("daily_workout", {})
    previous_workout = cached.get("workout_recommendation") if cached.get("date") == yesterday else None

    # Fetch Garmin data only if the user has connected their account.
    garmin_data: dict = {}
    has_garmin = bool(profile.get("garmin_tokens"))
    if has_garmin:
        try:
            garmin_data = garmin.fetch_daily_stats(user_id_str, force_refresh=True)
        except GarminConnectAuthenticationError:
            await bot.send_message(
                chat_id,
                "Couldn't connect to Garmin — run /connect_garmin to re-link your account.",
            )
            return
        except Exception as exc:
            logger.error("Garmin fetch error: %s", exc)
            await bot.send_message(chat_id, "Had trouble fetching your Garmin data. Try again in a moment.")
            return

    workout_history: list[dict] = profile.get("workout_history", [])

    try:
        result = get_workout_recommendation(
            garmin_data, profile, weather, history, previous_workout, workout_history
        )
    except Exception as exc:
        logger.error("Workout recommendation error: %s", exc)
        await bot.send_message(chat_id, "Had trouble generating your recommendation. Try again in a moment.")
        return

    # Save daily workout and append to history in a single DynamoDB write.
    storage.save_daily_workout_and_history(user_id_str, result, today)

    name = profile.get("name", "there")
    tier = result["recovery_tier"]

    if has_garmin and tier:
        emoji = _RECOVERY_EMOJI.get(tier, "⚪")
        recovery_line = _build_recovery_line(garmin_data, tier)
        briefing_text = (
            f"Good morning {name}! 🌅\n"
            f"{emoji} {recovery_line}\n"
            f"{result['summary']}\n"
            f"💪 {result['motivation']}"
        )
    else:
        briefing_text = (
            f"Good morning {name}! 🌅\n"
            f"{result['summary']}\n"
            f"💪 {result['motivation']}"
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
