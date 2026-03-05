"""
Shared authorization helpers for the fitness coach bot.

Parses ALLOWED_TELEGRAM_USER_IDS from the environment and exposes is_allowed().
Format: "123456:Yoav_Geva,789012:John_Doe"  (underscore as space separator in names)
"""
import logging
import os

from dotenv import load_dotenv

load_dotenv()  # must run before _parse_allowed_users() reads os.environ

logger = logging.getLogger(__name__)


def _parse_allowed_users() -> dict[int, str]:
    """Parse ALLOWED_TELEGRAM_USER_IDS into {user_id: display_name}."""
    raw = os.environ.get("ALLOWED_TELEGRAM_USER_IDS", "")
    result: dict[int, str] = {}
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        parts = entry.split(":", 1)
        uid_str = parts[0].strip()
        name = parts[1].replace("_", " ").strip() if len(parts) == 2 else uid_str
        if uid_str.lstrip("-").isdigit():
            result[int(uid_str)] = name
        else:
            logger.warning("auth: ignoring invalid user entry %r", entry)
    return result


# Loaded once at import time.
ALLOWED_USERS: dict[int, str] = _parse_allowed_users()


def is_allowed(user_id: int) -> bool:
    """Return True if user_id is in the allow-list (or list is empty = dev mode)."""
    if not ALLOWED_USERS:
        return True  # No restriction configured — allow all (dev mode)
    return user_id in ALLOWED_USERS
