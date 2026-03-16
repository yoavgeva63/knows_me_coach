"""
Shared utility helpers.

Import from here rather than from submodules:

    from utils import israel_now, israel_today
"""

from utils.time_utils import israel_now, israel_today, TZ_ISRAEL

__all__ = ["israel_now", "israel_today", "TZ_ISRAEL"]
