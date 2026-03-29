"""
Israel-time helpers.

All user-visible dates in this project use Israel local time (UTC+2, approximated
as a fixed offset — we accept the ±1 h DST inaccuracy for simplicity).

The server runs on UTC, so using date.today() or datetime.now(timezone.utc) directly
gives the wrong date between 00:00–02:00 AM Israel time.

Import via the package:

    from utils import israel_now, israel_today

If DST-awareness is ever needed, swap the implementation here to use
`zoneinfo.ZoneInfo("Asia/Jerusalem")` — no other files change.
"""

from datetime import datetime, timezone
import zoneinfo

TZ_ISRAEL = zoneinfo.ZoneInfo("Asia/Jerusalem")

def israel_now() -> datetime:
    """Return the current datetime in Israel time (timezone-aware)."""
    return datetime.now(timezone.utc).astimezone(TZ_ISRAEL)


def israel_today() -> str:
    """Return today's date in Israel time as an ISO string (YYYY-MM-DD)."""
    return israel_now().strftime("%Y-%m-%d")
