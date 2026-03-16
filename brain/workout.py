"""
Workout briefing Claude call.

Uses forced tool use so the response is always a structured dict — no free-text
parsing required. Called by workout_recommender.get_workout_recommendation().
"""

from brain._client import _get_client
from brain._context import clean_history


_WORKOUT_BRIEFING_TOOL = {
    "name": "workout_briefing",
    "description": "Return the structured morning workout briefing.",
    "input_schema": {
        "type": "object",
        "properties": {
            "summary": {
                "type": "string",
                "description": (
                    "One sentence: workout type and effort level (e.g. 'Tempo run 8 km — hard effort', "
                    "'Push day — heavy, ~60 min'), or 'Rest day — [brief reason]' if a rest day is recommended. "
                    "No RPE numbers."
                ),
            },
            "motivation": {
                "type": "string",
                "description": "One motivational sentence tied to the athlete's goal or recent context",
            },
            "workout_recommendation": {
                "type": "string",
                "description": (
                    "If training: full workout detail — starts directly with the workout title, no greeting. "
                    "Ends with a one-line coaching note on recovery and load. "
                    "If rest day: 2–3 sentences on what to do instead (sleep, walk, stretch) and why it serves the goal."
                ),
            },
        },
        "required": ["summary", "motivation", "workout_recommendation"],
    },
}


def get_workout_briefing(
    prompt: str,
    conversation_history: list[dict] | None = None,
) -> dict:
    """Call Claude with forced tool use to get a structured workout briefing.

    Args:
        prompt:               The fully-assembled workout context prompt.
        conversation_history: Prior conversation messages for context.

    Returns:
        Dict with keys: summary, motivation, workout_recommendation.
    """
    client = _get_client()
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=700,
        system="You are a personal fitness coach writing a daily morning briefing.",
        tools=[_WORKOUT_BRIEFING_TOOL],
        tool_choice={"type": "tool", "name": "workout_briefing"},
        messages=clean_history(conversation_history or []) + [{"role": "user", "content": prompt}],
    )
    return response.content[0].input  # guaranteed dict matching the schema


def get_modified_workout(original_workout: str, user_request: str) -> dict:
    """Generate a revised workout based on the original plan and the user's modification request.

    Calls Claude with forced tool use so the response is always a structured dict.

    Args:
        original_workout: The full workout_recommendation text from today's cached plan.
        user_request:     The user's free-text description of what they want to change.

    Returns:
        Dict with keys: summary, motivation, workout_recommendation.
    """
    prompt = f"""You are a personal fitness coach. The athlete was given this workout today:

<original_workout>
{original_workout}
</original_workout>

The athlete wants to modify it. Their request:
"{user_request}"

Rewrite the workout to incorporate their request while keeping it sensible and safe.
Preserve the same general format and structure as the original.
Return the modified workout using the workout_briefing tool."""

    client = _get_client()
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=700,
        system="You are a personal fitness coach adapting a workout to the athlete's needs.",
        tools=[_WORKOUT_BRIEFING_TOOL],
        tool_choice={"type": "tool", "name": "workout_briefing"},
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].input  # guaranteed dict matching the schema
