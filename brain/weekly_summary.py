"""
Weekly summary Coach's Take — Claude call.

Receives pre-built text blocks (training, nutrition, recovery) plus the week's
chat history and coach notes, and returns a single coaches_take string.

All other sections of the weekly message (training list, nutrition averages,
recovery stats) are built deterministically in weekly_briefing.py — Claude is
only responsible for the insight paragraph at the bottom.
"""

from brain._client import _get_client


_COACHES_TAKE_TOOL = {
    "name": "coaches_take",
    "description": "Return the coach's weekly insight paragraph.",
    "input_schema": {
        "type": "object",
        "properties": {
            "coaches_take": {
                "type": "string",
                "description": (
                    "2–4 sentences covering: what went well this week, what to focus on more, "
                    "and one concrete goal for next week. "
                    "Be direct and personal — reference specific workouts or numbers where relevant. "
                    "End with the next-week focus as the final sentence."
                ),
            },
        },
        "required": ["coaches_take"],
    },
}


def get_weekly_coaches_take(
    training_block: str,
    nutrition_block: str,
    recovery_block: str,
    week_chat_history: list[dict],
    coach_notes: list[dict],
) -> str:
    """Call Claude to generate the Coach's Take paragraph for the weekly summary.

    Args:
        training_block:    Pre-built training section text (day-by-day with statuses).
        nutrition_block:   Pre-built nutrition section text (averages or low-data notice).
        recovery_block:    Pre-built recovery section text, or empty string if no Garmin.
        week_chat_history: This week's conversation messages filtered by ts >= sunday.
                           Each dict has 'role' and 'content' keys.
        coach_notes:       Long-term coach notes from the user's profile.

    Returns:
        The coaches_take string ready to embed in the weekly message.
    """
    client = _get_client()

    notes_text = ""
    if coach_notes:
        notes_lines = [f"- {n['note']}" for n in coach_notes[-10:]]
        notes_text = "Coach notes about this user:\n" + "\n".join(notes_lines)

    chat_text = ""
    if week_chat_history:
        lines = []
        for msg in week_chat_history[-30:]:
            role = "User" if msg.get("role") == "user" else "Coach"
            lines.append(f"{role}: {msg.get('content', '')[:200]}")
        chat_text = "This week's conversation highlights:\n" + "\n".join(lines)

    prompt_parts = [
        "Here is the user's week at a glance. Write the Coach's Take paragraph.\n",
        training_block,
        nutrition_block,
    ]
    if recovery_block:
        prompt_parts.append(recovery_block)
    if notes_text:
        prompt_parts.append(notes_text)
    if chat_text:
        prompt_parts.append(chat_text)

    prompt = "\n\n".join(part for part in prompt_parts if part)

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=300,
        system="You are a personal fitness coach writing the weekly insight for your athlete.",
        tools=[_COACHES_TAKE_TOOL],
        tool_choice={"type": "tool", "name": "coaches_take"},
        messages=[{"role": "user", "content": prompt}],
    )
    try:
        return response.content[0].input["coaches_take"]
    except (IndexError, KeyError, AttributeError) as exc:
        raise RuntimeError(f"Unexpected coaches_take response structure: {exc}") from exc
