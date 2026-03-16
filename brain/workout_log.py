"""
Workout completion logging — Claude call for the ConversationHandler path.

When a user taps the ✏️ Modify button and describes what they will do differently,
this module interprets the free-text description and returns a structured result
that storage.update_workout_status() can persist.

The natural-language path (user says "I changed my workout" in chat) is handled
directly by the log_workout_status ACTION_TOOL in brain/conversation.py — no
second Claude call is needed there since Claude already understands the context.
"""

from brain._client import _get_client


_INTERPRET_TOOL = {
    "name": "interpret_modification",
    "description": "Interpret the user's workout modification plan and return structured data.",
    "input_schema": {
        "type": "object",
        "properties": {
            "actual_summary": {
                "type": "string",
                "description": (
                    "A concise one-line summary of what the user will actually do, "
                    "e.g. 'Upper body — 3 sets per exercise, skipping lunges' or "
                    "'5km easy run instead of push day'."
                ),
            },
            "actual_type": {
                "type": "string",
                "enum": ["same_modified", "different"],
                "description": (
                    "'same_modified' if the user is tweaking the recommended workout "
                    "(fewer sets, swapped exercises, shorter duration, etc.). "
                    "'different' if they are doing a completely different type of session."
                ),
            },
        },
        "required": ["actual_summary", "actual_type"],
    },
}


def interpret_workout_modification(original_summary: str, user_plan: str) -> dict:
    """Interpret a free-text workout modification plan and return structured data.

    Called by the ⚡ Modify ConversationHandler after the user describes what
    they will do instead of the recommended workout.

    Args:
        original_summary: The one-line summary of the morning's recommended workout.
        user_plan:        The user's free-text description of what they plan to do.

    Returns:
        Dict with keys:
          - actual_summary (str): concise one-liner of the actual session.
          - actual_type (str): "same_modified" or "different".
    """
    client = _get_client()
    prompt = (
        f"Recommended workout: {original_summary}\n"
        f"User's plan: {user_plan}\n\n"
        "Interpret the user's plan and return a structured result."
    )
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=200,
        system="You are a fitness coach assistant interpreting a user's workout modification.",
        tools=[_INTERPRET_TOOL],
        tool_choice={"type": "tool", "name": "interpret_modification"},
        messages=[{"role": "user", "content": prompt}],
    )
    try:
        return response.content[0].input
    except (IndexError, AttributeError) as exc:
        raise RuntimeError(f"Unexpected interpret_modification response structure: {exc}") from exc
