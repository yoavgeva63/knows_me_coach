"""
Long-term memory extraction from conversation history.

Uses Haiku (cost-efficient) to scan messages being dropped from the conversation
window and extract facts worth persisting as long-term coach notes.
"""

from brain._client import _get_client


def extract_memorable_facts(
    messages: list[dict],
    existing_notes: list[dict],
) -> list[str]:
    """Scan messages being dropped from conversation history and extract facts
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

    client = _get_client()

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
