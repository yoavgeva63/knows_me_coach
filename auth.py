"""
Shared authorization helpers for the fitness coach bot.

Loads allowed users from allowed_users.json (committed, non-secret config).
Falls back to ALLOWED_TELEGRAM_USER_IDS env var for backwards compatibility.
"""
import json
import logging
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

_ALLOWED_USERS_FILE = Path(__file__).parent / "allowed_users.json"


def _load_allowed_users() -> dict[int, str]:
    """Load allowed users from allowed_users.json, falling back to env var."""
    if _ALLOWED_USERS_FILE.exists():
        with _ALLOWED_USERS_FILE.open() as f:
            data: dict[str, str] = json.load(f)
        return {int(uid): name for uid, name in data.items()}

    # Legacy fallback: ALLOWED_TELEGRAM_USER_IDS="123:Yoav_Geva,456:John_Doe"
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
ALLOWED_USERS: dict[int, str] = _load_allowed_users()


def is_allowed(user_id: int) -> bool:
    """Return True if user_id is in the allow-list (or list is empty = dev mode)."""
    if not ALLOWED_USERS:
        return True  # No restriction configured — allow all (dev mode)
    return user_id in ALLOWED_USERS
