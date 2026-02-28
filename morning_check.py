"""
Morning alarm checker — run every 15 minutes via cron on the Ubuntu server.

Cron entry (edit with: crontab -e):
    */15 * * * * cd /path/to/project && /path/to/venv/bin/python morning_check.py >> /var/log/morning_check.log 2>&1

Alarm fires at the first 15-minute boundary >= the configured time.
Example: alarm set to 09:00 → fires at 09:00. Set to 09:07 → fires at 09:15.
"""
import asyncio
import logging
import os
from datetime import datetime, timezone, timedelta

from dotenv import load_dotenv
from telegram import Bot

import garmin_daily_stats
import storage
from briefing import send_morning_briefing

load_dotenv()

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

_ISRAEL_UTC_OFFSET_H = 2
_SLEEP_MODE_FALLBACK = "10:00"


async def main() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    user_id_str = os.environ.get("ALLOWED_TELEGRAM_USER_ID", "")
    if not token or not user_id_str:
        logger.error("TELEGRAM_BOT_TOKEN and ALLOWED_TELEGRAM_USER_ID must be set in .env")
        return

    chat_id = int(user_id_str)

    now_utc = datetime.now(timezone.utc)
    now_israel = now_utc + timedelta(hours=_ISRAEL_UTC_OFFSET_H)
    today_israel = now_israel.strftime("%Y-%m-%d")
    now_hhmm = now_israel.strftime("%H:%M")

    prefs = storage.get_morning_prefs(user_id_str)

    if prefs["sent_date"] == today_israel:
        logger.info("Already sent today (%s), skipping.", today_israel)
        return

    alarm_time = prefs["alarm_time"]  # "HH:MM" or "sleep"
    should_send = False

    if alarm_time == "sleep":
        try:
            garmin_data = garmin_daily_stats.fetch_daily_stats(force_refresh=True)
            wake_utc_str = (garmin_data or {}).get("sleep", {}).get("wake_time_utc")
            if wake_utc_str:
                wake_utc = datetime.fromisoformat(wake_utc_str)
                wake_israel = wake_utc + timedelta(hours=_ISRAEL_UTC_OFFSET_H)
                if wake_israel.strftime("%Y-%m-%d") == today_israel and now_utc >= wake_utc:
                    logger.info("Garmin wake time detected at %s (Israel), sending.", wake_israel.strftime("%H:%M"))
                    should_send = True
            if not should_send and now_hhmm >= _SLEEP_MODE_FALLBACK:
                logger.info("Sleep mode fallback reached (%s), sending.", _SLEEP_MODE_FALLBACK)
                should_send = True
        except Exception as exc:
            logger.error("Garmin fetch failed during sleep check: %s", exc)
            if now_hhmm >= _SLEEP_MODE_FALLBACK:
                should_send = True
    else:
        if now_hhmm >= alarm_time:
            logger.info("Alarm time %s reached (now %s), sending.", alarm_time, now_hhmm)
            should_send = True

    if should_send:
        profile_path = os.path.join(os.path.dirname(__file__), "user_profile.json")
        bot = Bot(token=token)
        await send_morning_briefing(bot, chat_id, user_id_str, profile_fallback_path=profile_path)


if __name__ == "__main__":
    asyncio.run(main())
