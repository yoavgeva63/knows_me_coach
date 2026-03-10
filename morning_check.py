"""
Morning alarm checker — run every 15 minutes via cron on the Ubuntu server.

Iterates all users in DynamoDB and sends each one their morning briefing when
their configured alarm time is reached (or when Garmin detects a wake-up).

Cron entry (edit with: crontab -e):
    */15 * * * * cd /home/ubuntu/knows_me_coach && /home/ubuntu/knows_me_coach/venv/bin/python morning_check.py >> /home/ubuntu/knows_me_coach/morning_check.log 2>&1

Alarm fires at the first 15-minute boundary >= the configured time.
Example: alarm set to 09:00 → fires at 09:00. Set to 09:07 → fires at 09:15.
"""
import asyncio
import logging
import os
from datetime import datetime, timezone, timedelta

from dotenv import load_dotenv

load_dotenv()  # must run before local imports so AWS env vars are set before boto3 initialises

from telegram import Bot

import garmin
import storage
from briefing import send_morning_briefing

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

_ISRAEL_UTC_OFFSET_H = 2
_SLEEP_MODE_FALLBACK = "09:30"


async def _check_and_send(
    bot: Bot,
    user_id_str: str,
    now_utc: datetime,
    today_israel: str,
    now_hhmm: str,
) -> None:
    """Check alarm conditions for one user and send the briefing if due."""
    prefs = storage.get_morning_prefs(user_id_str)

    if prefs["sent_date"] == today_israel:
        logger.info("Already sent today for user %s, skipping.", user_id_str)
        return

    alarm_time = prefs["alarm_time"]  # "HH:MM" or "sleep"
    should_send = False

    if alarm_time == "sleep":
        try:
            garmin_data = garmin.fetch_daily_stats(user_id_str, force_refresh=True)
            garmin_dict = garmin_data or {}

            # 1. Official Garmin wake time from sleep summary.
            #    Require wake_time >= 05:00 local time to ignore brief nighttime wakings.
            wake_utc_str = garmin_dict.get("sleep", {}).get("wake_time_utc")
            has_early_wake = False  # woke before 05:00 — likely a bathroom trip, not morning
            if wake_utc_str:
                wake_utc = datetime.fromisoformat(wake_utc_str)
                wake_israel = wake_utc + timedelta(hours=_ISRAEL_UTC_OFFSET_H)
                if wake_israel.strftime("%Y-%m-%d") == today_israel and now_utc >= wake_utc:
                    if wake_israel.hour >= 5:
                        logger.info(
                            "Garmin wake detected for %s at %s (Israel).",
                            user_id_str, wake_israel.strftime("%H:%M"),
                        )
                        should_send = True
                    else:
                        has_early_wake = True
                        logger.info(
                            "Garmin wake at %s is before 05:00 — treating as nighttime waking, skipping.",
                            wake_israel.strftime("%H:%M"),
                        )

            # 2. Step-based fallback: steps sync faster than sleep summaries.
            #    Uses recent_steps (last 60 min) instead of total daily steps so that
            #    steps taken at 01:00 AM don't trigger the briefing at 06:00.
            #    Window 06:00–16:00 prevents false positives from late-night activity
            #    when sleep data hasn't synced yet (has_early_wake would be False).
            if not should_send and not has_early_wake and "06:00" <= now_hhmm <= "16:00":
                steps_info = garmin_dict.get("steps", {})
                recent_steps = steps_info.get("recent_steps", 0) if isinstance(steps_info, dict) else 0
                if recent_steps > 200:
                    logger.info(
                        "Step-based wake detected for %s (%d recent steps in last 60 min).",
                        user_id_str, recent_steps,
                    )
                    should_send = True
            
            # 3. Hard fallback: send regardless if 09:30 has passed.
            if not should_send and now_hhmm >= _SLEEP_MODE_FALLBACK:
                logger.info("Sleep mode fallback reached for %s (%s).", user_id_str, now_hhmm)
                should_send = True
        except Exception as exc:
            logger.error("Garmin fetch failed for %s: %s", user_id_str, exc)
            if now_hhmm >= _SLEEP_MODE_FALLBACK:
                should_send = True
    else:
        if now_hhmm >= alarm_time:
            logger.info("Alarm time %s reached for user %s (now %s).", alarm_time, user_id_str, now_hhmm)
            should_send = True

    if should_send:
        await send_morning_briefing(bot, int(user_id_str), user_id_str)


async def main() -> None:
    """Check all users and send morning briefings where conditions are met."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        logger.error("TELEGRAM_BOT_TOKEN must be set in .env")
        return

    all_user_ids = storage.list_all_user_ids()
    if not all_user_ids:
        logger.info("No users found in database.")
        return

    now_utc = datetime.now(timezone.utc)
    now_israel = now_utc + timedelta(hours=_ISRAEL_UTC_OFFSET_H)
    today_israel = now_israel.strftime("%Y-%m-%d")
    now_hhmm = now_israel.strftime("%H:%M")

    bot = Bot(token=token)
    for user_id_str in all_user_ids:
        try:
            await _check_and_send(bot, user_id_str, now_utc, today_israel, now_hhmm)
        except Exception as exc:
            logger.error("Unexpected error for user %s: %s", user_id_str, exc)


if __name__ == "__main__":
    asyncio.run(main())
