"""
Main conversation handler — the coach's day-to-day chat with the user.

Builds a rich system prompt from the user's profile, Garmin data, weather, and
cached workout, then calls Claude with the full ACTION_TOOLS set so it can
trigger side-effects (set alarm, remember fact, etc.) within a single response.
"""

from datetime import datetime, timezone

from brain._client import _get_client
from brain._context import clean_history, format_garmin_context, format_nutrition_context, format_profile_context


SYSTEM_PROMPT = """You are a proactive, knowledgeable, and motivating personal fitness and health coach \
delivered via Telegram. Your name is Coach.

Your core responsibilities:
- Provide personalized workout plans based on the user's fitness level, goals, and recent activity
- Give nutrition advice and meal suggestions tailored to training load
- Interpret health metrics (sleep, HRV, resting heart rate, steps) and explain what they mean practically
- Send motivating morning briefings with a workout plan, meal suggestion, and motivational message
- Suggest running routes when asked
- Track diet and produce weekly meal rotations with shopping lists
- Give weekly health and progress summaries
- Proactively nudge the user when they haven't worked out

Your communication style:
- Concise and direct — Telegram messages should be punchy, not essays
- Use emojis sparingly but effectively (e.g. 💪 🏃 😴 🥗)
- Be encouraging but honest — don't sugarcoat lack of effort
- Ask clarifying questions when needed before giving advice
- Remember context from earlier in the conversation

When you don't have data (e.g. no Garmin sync yet) assume from the past and tell the user.
Always respond in the same language the user writes in.

Nutrition behaviour:
- Always treat any mentioned meal as ONE meal in a normal multi-meal day. Never assume it covers the whole day.
- When nutrition context is provided below, use it to calculate how many calories/macros remain and suggest accordingly.
- If no meals are logged yet and the user asks about food, ask what they've already eaten before recommending a full plan.

For destructive or complex actions, never execute directly — instead guide the user to confirm:
- Clear conversation history → "To erase our conversation history, tap /clear to confirm."
- Update profile fields (goal, weight, fitness level, etc.) → "To update your profile, tap /profile."
- Reconnect Garmin → "To relink your Garmin account, tap /connect_garmin."""


ACTION_TOOLS = [
    {
        "name": "set_morning_alarm",
        "description": (
            "Set the user's morning briefing alarm time. "
            "Use when the user asks to change, set, or update their morning alarm or briefing time."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "time": {
                    "type": "string",
                    "description": (
                        "HH:MM in Israel time (e.g. '07:00') or 'sleep' to trigger "
                        "automatically when Garmin detects the user has woken up."
                    ),
                }
            },
            "required": ["time"],
        },
    },
    {
        "name": "remember_fact",
        "description": (
            "Persist a long-term fact about the user that the coach should always remember. "
            "Use when the user explicitly asks to remember something, or shares a persistent "
            "personal detail like an injury, dietary restriction, or training constraint."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "fact": {
                    "type": "string",
                    "description": "Concise fact to store, e.g. 'has a sore left knee' or 'doesn't eat meat'.",
                }
            },
            "required": ["fact"],
        },
    },
    {
        "name": "trigger_morning_briefing",
        "description": (
            "Generate and send the full morning workout briefing to the user. "
            "Use only when the user explicitly asks for their morning briefing or today's workout plan."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "update_daily_workout",
        "description": (
            "Save the workout plan you just generated into today's cached workout. "
            "Call this whenever you generate or modify today's workout so the Workout button stays in sync. "
            "If no workout exists for today it will be created automatically."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "workout_recommendation": {
                    "type": "string",
                    "description": "The complete workout plan text to cache.",
                },
                "summary": {
                    "type": "string",
                    "description": "Optional one-line summary, e.g. 'Easy 30-min swim, RPE 5'.",
                },
            },
            "required": ["workout_recommendation"],
        },
    },
]


def get_claude_response(
    conversation_history: list[dict],
    user_message: str,
    weather: str = "",
    garmin_data: dict | None = None,
    user_profile: dict | None = None,
    daily_workout: dict | None = None,
    logged_meals: list[dict] | None = None,
) -> tuple[str, list[dict]]:
    """Send the conversation history + new user message to Claude and return the reply.

    Claude may also decide to call one or more action tools (set alarm, remember a fact,
    trigger the morning briefing). Tool calls are returned alongside the text so the caller
    can execute them as side effects — no second LLM call is required for action-only tools.

    Args:
        conversation_history: List of {"role": "user"|"assistant", "content": "..."} dicts.
        user_message: The latest message from the user.
        weather: Current weather string (injected into system prompt when available).
        garmin_data: Latest Garmin daily stats (injected into system prompt when available).
        user_profile: User profile dict including coach_notes (injected into system prompt).
        daily_workout: Today's cached workout plan (injected into system prompt when available).
        logged_meals: Today's logged meals from storage.get_meals_from_profile(). Pass only when
            the user's message is nutrition-related. None means skip injection entirely.

    Returns:
        Tuple of (reply_text, tool_calls) where tool_calls is a list of
        {"name": str, "input": dict} dicts, usually empty.
    """
    client = _get_client()

    system = SYSTEM_PROMPT
    if user_profile:
        system += f"\n\n{format_profile_context(user_profile)}"
    if weather and weather != "Weather unavailable":
        system += f"\n\nCurrent weather: {weather}"
    if garmin_data:
        fetch_time = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        system += f"\n\nLatest Garmin data (fetched at {fetch_time}):\n{format_garmin_context(garmin_data)}"
    if daily_workout and daily_workout.get("workout_recommendation"):
        system += f"\n\nToday's cached workout plan:\n{daily_workout['workout_recommendation']}"
    if logged_meals is not None and user_profile:
        system += f"\n\n{format_nutrition_context(user_profile, logged_meals)}"

    messages = clean_history(conversation_history + [{"role": "user", "content": user_message}])

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=system,
        tools=ACTION_TOOLS,
        tool_choice={"type": "auto"},
        messages=messages,
    )

    text = ""
    tool_calls = []
    for block in response.content:
        if hasattr(block, "text"):
            text = block.text
        elif block.type == "tool_use":
            tool_calls.append({"name": block.name, "input": block.input})

    return text, tool_calls
