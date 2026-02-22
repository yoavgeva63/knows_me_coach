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

When you don't have data (e.g. no Garmin sync yet), ask the user to fill in the gaps manually \
and note that passive tracking will be connected soon.

Always respond in the same language the user writes in."""


def get_morning_briefing(garmin_data: dict, user_profile: dict, weather: str = "") -> str:
    """Generate a personalised morning briefing from Garmin data and user profile.

    Args:
        garmin_data:  Output of garmin.fetch_daily_stats().
        user_profile: Contents of user_profile.json.
        weather:      One-line weather string from wttr.in (optional).

    Returns:
        A short, Telegram-formatted morning message from Claude.
    """
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    sleep = garmin_data.get("sleep", {})
    hrv = garmin_data.get("hrv", {})
    steps = garmin_data.get("steps", {})
    activity = garmin_data.get("last_activity", {})

    # Format seconds → h m for readability in the prompt
    def _fmt_seconds(s):
        if not isinstance(s, (int, float)):
            return "N/A"
        h, m = divmod(int(s) // 60, 60)
        return f"{h}h {m}m"

    prompt = f"""You are a personal fitness coach. Generate a morning briefing for your athlete.

## Athlete Profile
- Name: {user_profile.get('name', 'Athlete')}
- Age: {user_profile.get('age', 'N/A')}
- Goal: {user_profile.get('fitness_goal', 'general fitness')}
- Level: {user_profile.get('fitness_level', 'intermediate')}
- Target workouts/week: {user_profile.get('workouts_per_week', 5)}
- Weight: {user_profile.get('weight_kg', 'N/A')} kg

## Last Night's Recovery Data (from Garmin)
- Sleep score: {sleep.get('sleep_score', 'N/A')} / 100
- Total sleep: {_fmt_seconds(sleep.get('total_sleep_seconds'))}
- Deep sleep: {_fmt_seconds(sleep.get('deep_sleep_seconds'))}
- REM sleep: {_fmt_seconds(sleep.get('rem_sleep_seconds'))}
- HRV last night: {hrv.get('last_night_avg', 'N/A')} ms
- HRV weekly avg: {hrv.get('weekly_avg', 'N/A')} ms
- HRV status: {hrv.get('status', 'N/A')}
- Steps yesterday: {steps.get('total_steps', 'N/A')}

## Last Workout
- Name: {activity.get('name', 'N/A')}
- Type: {activity.get('type', 'N/A')}
- When: {activity.get('start_time', 'N/A')}
- Duration: {_fmt_seconds(activity.get('duration_seconds'))}
- Distance: {round(activity.get('distance_meters', 0) / 1000, 1) if activity.get('distance_meters') else 'N/A'} km
- Avg HR: {activity.get('avg_hr', 'N/A')} bpm
- Calories: {activity.get('calories', 'N/A')}

## Current Weather
{weather if weather else "N/A"}

## Recovery Decision Rules
- If sleep score < 60 OR HRV last night < 80% of weekly avg → recommend a light/rest day
- Otherwise → recommend a full training session suited to the athlete's goal and level

## Required Output Format
Write ONLY the message below — no preamble, no explanation. Keep it under 200 words.

Good morning {user_profile.get('name', 'Champ')} ☀️

😴 Recovery: <1–2 sentences on last night's sleep and HRV>
🌤 Weather: <current conditions and any relevant note for outdoor training>
💪 Today's workout: <specific recommendation with sets/reps or duration — adjusted for recovery and weather>
🥗 Meal suggestion: <one practical meal idea that supports today's training>
🔥 Motivation: <one punchy motivational sentence>"""

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=600,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text


def get_claude_response(
    conversation_history: list[dict], user_message: str, weather: str = ""
) -> str:
    """
    Send the conversation history + new user message to Claude and return the reply.

    Args:
        conversation_history: List of {"role": "user"|"assistant", "content": "..."} dicts
        user_message: The latest message from the user
        weather: Current weather string (injected into system prompt when available)

    Returns:
        Claude's reply as a string
    """
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    system = SYSTEM_PROMPT
    if weather and weather != "Weather unavailable":
        system += f"\n\nCurrent weather: {weather}"

    messages = conversation_history + [{"role": "user", "content": user_message}]

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=system,
        messages=messages,
    )

    return response.content[0].text
