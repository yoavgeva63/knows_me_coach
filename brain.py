"""
Claude AI brain for the fitness coach bot.
Manages conversation history and sends messages to Claude.
"""
import os
import anthropic

# System prompt that defines the coach's personality and capabilities
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
Always respond in the same language the user writes in."""


def _fmt_seconds(s) -> str:
    if not isinstance(s, (int, float)):
        return "N/A"
    h, m = divmod(int(s) // 60, 60)
    return f"{h}h {m}m"


def _format_garmin_context(garmin_data: dict) -> str:
    sleep = garmin_data.get("sleep", {})
    hrv = garmin_data.get("hrv", {})
    steps = garmin_data.get("steps", {})
    activity = garmin_data.get("last_activity", {})
    recent = garmin_data.get("recent_activities", [])

    lines = [
        f"Date: {garmin_data.get('date', 'N/A')}",
        f"Sleep score: {sleep.get('sleep_score', 'N/A')}/100, "
        f"total: {_fmt_seconds(sleep.get('total_sleep_seconds'))}, "
        f"deep: {_fmt_seconds(sleep.get('deep_sleep_seconds'))}, "
        f"REM: {_fmt_seconds(sleep.get('rem_sleep_seconds'))}",
        f"HRV: {hrv.get('last_night_avg', 'N/A')} ms (weekly avg {hrv.get('weekly_avg', 'N/A')} ms, status: {hrv.get('status', 'N/A')})",
        f"Steps today: {steps.get('total_steps', 'N/A')}",
        f"Last workout: {activity.get('name', 'N/A')} ({activity.get('type', 'N/A')}) "
        f"on {activity.get('start_time', 'N/A')}, "
        f"duration {_fmt_seconds(activity.get('duration_seconds'))}, "
        f"avg HR {activity.get('avg_hr', 'N/A')} bpm",
    ]

    if recent:
        recent_summary = "; ".join(
            f"{a.get('name', 'N/A')} {_fmt_seconds(a.get('duration_seconds'))} on {a.get('start_time', 'N/A')}"
            for a in recent[:5]
        )
        lines.append(f"Recent activities (last 5): {recent_summary}")

    return "\n".join(lines)


def _format_profile_context(user_profile: dict) -> str:
    """Format user profile + coach notes for injection into the system prompt."""
    lines = [
        "## User Profile",
        f"Name: {user_profile.get('name', 'N/A')} | "
        f"Age: {user_profile.get('age', 'N/A')} | "
        f"Weight: {user_profile.get('weight_kg', 'N/A')} kg | "
        f"Level: {user_profile.get('fitness_level', 'N/A')}",
        f"Primary goal: {user_profile.get('primary_goal', user_profile.get('fitness_goal', 'N/A'))}",
        f"Secondary goal: {user_profile.get('secondary_goal', 'N/A')}",
        f"Training days/week: {user_profile.get('weekly_training_days', user_profile.get('workouts_per_week', 'N/A'))} | "
        f"Session duration: {user_profile.get('preferred_session_duration_minutes', 'N/A')} min",
    ]

    target = user_profile.get("target_event", {})
    if target and target.get("name"):
        lines.append(f"Target event: {target['name']} on {target.get('date', 'TBD')}")

    notes = user_profile.get("coach_notes", [])
    if notes:
        lines.append("\n## Coach Notes (long-term memory)")
        for n in notes:
            lines.append(f"- {n.get('date', '?')}: {n.get('note', '')}")

    return "\n".join(lines)


def extract_memorable_facts(
    messages: list[dict],
    existing_notes: list[dict],
) -> list[str]:
    """
    Scan messages being dropped from conversation history and extract facts
    worth persisting as long-term coach notes.

    Uses Haiku for cost efficiency since this is a focused extraction task.

    Args:
        messages: The oldest messages about to be discarded from history.
        existing_notes: Current coach_notes list (to avoid duplicates).

    Returns:
        A list of 0–3 concise fact strings, or [] if nothing notable.
    """
    if not messages:
        return []

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    existing_text = (
        "\n".join(f"- {n.get('note', '')}" for n in existing_notes)
        if existing_notes
        else "None"
    )
    conversation_text = "\n".join(
        f"{m['role'].upper()}: {m['content']}" for m in messages
    )

    prompt = f"""You are reviewing a conversation between a fitness coach AI and its user.
These messages are about to be deleted from short-term memory.
Extract any facts a personal coach should remember long-term.

Only extract facts that are:
- Physical limitations or injuries (e.g. "has a sore left knee")
- Fitness goals or target events (e.g. "training for Tel Aviv Marathon in April")
- Dietary restrictions or strong patterns (e.g. "doesn't eat meat", "skips breakfast regularly")
- Training preferences or hard constraints (e.g. "can only train before 8am")
- Explicit commitments (e.g. "committed to running 3x per week")

Do NOT extract:
- Casual chitchat or one-off mood
- Things already covered by existing notes below
- Generic advice that was given
- Anything vague or situational

Existing coach notes (don't duplicate):
{existing_text}

Conversation to review:
{conversation_text}

Reply with a bullet list (one fact per line, starting with -), or reply with exactly "NONE".
Keep each fact under 15 words. Maximum 3 facts."""

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": prompt}],
    )

    raw = response.content[0].text.strip()
    if raw.upper() == "NONE" or not raw:
        return []

    facts = []
    for line in raw.splitlines():
        line = line.lstrip("-• ").strip()
        if line:
            facts.append(line)
    return facts[:3]


_WORKOUT_BRIEFING_TOOL = {
    "name": "workout_briefing",
    "description": "Return the structured morning workout briefing.",
    "input_schema": {
        "type": "object",
        "properties": {
            "summary": {
                "type": "string",
                "description": "One sentence: workout type and RPE",
            },
            "motivation": {
                "type": "string",
                "description": "One motivational sentence tied to the athlete's goal or recent context",
            },
            "full_recommendation": {
                "type": "string",
                "description": "Full workout detail — starts directly with the workout title, no greeting. Ends with a one-line recovery coaching note.",
            },
        },
        "required": ["summary", "motivation", "full_recommendation"],
    },
}


def _clean_history(conversation_history: list[dict]) -> list[dict]:
    """Strip internal fields (e.g. ts) — the Anthropic API only accepts role + content."""
    return [
        {"role": m["role"], "content": f"[{m['ts']}] {m['content']}" if "ts" in m else m["content"]}
        for m in conversation_history
    ]


def get_workout_briefing(
    prompt: str,
    conversation_history: list[dict] | None = None,
) -> dict:
    """Call Claude with forced tool use to get a structured workout briefing.

    Args:
        prompt:               The fully-assembled workout context prompt.
        conversation_history: Prior conversation messages for context.

    Returns:
        Dict with keys: summary, motivation, full_recommendation.
    """
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=700,
        system="You are a personal fitness coach writing a daily morning briefing.",
        tools=[_WORKOUT_BRIEFING_TOOL],
        tool_choice={"type": "tool", "name": "workout_briefing"},
        messages=_clean_history(conversation_history or []) + [{"role": "user", "content": prompt}],
    )
    return response.content[0].input  # guaranteed dict matching the schema


def get_claude_response(
    conversation_history: list[dict],
    user_message: str,
    weather: str = "",
    garmin_data: dict | None = None,
    user_profile: dict | None = None,
) -> str:
    """
    Send the conversation history + new user message to Claude and return the reply.

    Args:
        conversation_history: List of {"role": "user"|"assistant", "content": "..."} dicts
        user_message: The latest message from the user
        weather: Current weather string (injected into system prompt when available)
        garmin_data: Latest Garmin daily stats (injected into system prompt when available)
        user_profile: User profile dict including coach_notes (injected into system prompt)

    Returns:
        Claude's reply as a string
    """
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    system = SYSTEM_PROMPT
    if user_profile:
        system += f"\n\n{_format_profile_context(user_profile)}"
    if weather and weather != "Weather unavailable":
        system += f"\n\nCurrent weather: {weather}"
    if garmin_data:
        system += f"\n\nLatest Garmin data:\n{_format_garmin_context(garmin_data)}"

    messages = _clean_history(conversation_history + [{"role": "user", "content": user_message}])

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=system,
        messages=messages,
    )

    return response.content[0].text
