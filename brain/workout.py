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
                "description": "One sentence: workout type and RPE",
            },
            "motivation": {
                "type": "string",
                "description": "One motivational sentence tied to the athlete's goal or recent context",
            },
            "workout_recommendation": {
                "type": "string",
                "description": (
                    "Full workout detail — starts directly with the workout title, no greeting. "
                    "Ends with a one-line recovery coaching note."
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
