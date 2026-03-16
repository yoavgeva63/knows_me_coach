"""
brain — all Claude API calls for the fitness coach bot.

Submodules:
  conversation   — main coach chat: SYSTEM_PROMPT, ACTION_TOOLS, get_claude_response
  workout        — structured morning briefing: get_workout_briefing
  nutrition      — meal suggestions: get_meal_suggestions, get_ingredient_meal
  memory         — long-term fact extraction: extract_memorable_facts
  workout_log    — workout completion interpreter: interpret_workout_modification
  weekly_summary — weekly Coach's Take: get_weekly_coaches_take
  _client        — shared Anthropic client singleton (internal)
  _context       — shared context-formatting helpers (internal)

All public symbols are re-exported here so callers can simply do:
    from brain import get_claude_response, get_workout_briefing
"""

from brain.conversation import ACTION_TOOLS, SYSTEM_PROMPT, get_claude_response
from brain.memory import extract_memorable_facts
from brain.nutrition import get_ingredient_meal, get_meal_suggestions
from brain.workout import get_workout_briefing
from brain.workout_log import interpret_workout_modification
from brain.weekly_summary import get_weekly_coaches_take

__all__ = [
    "SYSTEM_PROMPT",
    "ACTION_TOOLS",
    "get_claude_response",
    "get_workout_briefing",
    "get_meal_suggestions",
    "get_ingredient_meal",
    "extract_memorable_facts",
    "interpret_workout_modification",
    "get_weekly_coaches_take",
]
