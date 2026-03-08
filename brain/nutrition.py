"""
Nutrition-related Claude calls — meal suggestions and ingredient-based meals.

Both functions use forced tool use so responses are always structured dicts.
Called by nutrition_handlers.py after prompts are assembled by nutrition.py.
"""

from brain._client import _get_client


_MEAL_SUGGESTION_TOOL = {
    "name": "meal_suggestions",
    "description": "Return exactly two meal suggestions with full macro breakdown.",
    "input_schema": {
        "type": "object",
        "properties": {
            "options": {
                "type": "array",
                "minItems": 2,
                "maxItems": 2,
                "items": {
                    "type": "object",
                    "properties": {
                        "name":      {"type": "string",  "description": "Meal name, e.g. 'Greek Yogurt Bowl'"},
                        "kcal":      {"type": "integer", "description": "Total calories"},
                        "protein_g": {"type": "integer", "description": "Protein in grams"},
                        "fat_g":     {"type": "integer", "description": "Fat in grams"},
                        "carbs_g":   {"type": "integer", "description": "Carbohydrates in grams"},
                        "time_min":  {"type": "integer", "description": "Preparation time in minutes"},
                        "reasoning": {"type": "string",  "description": "One specific sentence explaining why this meal fits the user's goals"},
                    },
                    "required": ["name", "kcal", "protein_g", "fat_g", "carbs_g", "time_min", "reasoning"],
                },
            }
        },
        "required": ["options"],
    },
}

_INGREDIENT_MEAL_TOOL = {
    "name": "ingredient_meal",
    "description": "Return one meal built from the user's available ingredients.",
    "input_schema": {
        "type": "object",
        "properties": {
            "name":        {"type": "string",  "description": "Meal name"},
            "kcal":        {"type": "integer", "description": "Total calories"},
            "protein_g":   {"type": "integer", "description": "Protein in grams"},
            "fat_g":       {"type": "integer", "description": "Fat in grams"},
            "carbs_g":     {"type": "integer", "description": "Carbohydrates in grams"},
            "time_min":    {"type": "integer", "description": "Preparation time in minutes"},
            "uses":        {
                "type": "array",
                "items": {"type": "string"},
                "description": "Main ingredients used from the user's list",
            },
            "prep_method": {"type": "string",  "description": "2–3 sentence preparation instructions"},
            "reasoning":   {"type": "string",  "description": "One specific sentence on why this fits the user's goals"},
            "tip":         {"type": "string",  "description": "One sentence on 1–2 missing items that would most improve the meal's nutrition, or empty string if none"},
        },
        "required": ["name", "kcal", "protein_g", "fat_g", "carbs_g", "time_min", "uses", "prep_method", "reasoning", "tip"],
    },
}


def get_meal_suggestions(prompt: str) -> list[dict]:
    """Call Claude to generate exactly two meal options for a given meal slot.

    Uses forced tool use so the response is always a structured dict — no free-text
    parsing required. Called by nutrition_handlers.py's callback handler.

    Args:
        prompt: Fully assembled prompt from nutrition.build_meal_suggestion_prompt().

    Returns:
        List of 2 meal dicts, each with: name, kcal, protein_g, fat_g, carbs_g,
        time_min, reasoning.
    """
    client = _get_client()
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=900,
        system="You are a precise sports nutritionist. Always return exactly 2 options as instructed.",
        tools=[_MEAL_SUGGESTION_TOOL],
        tool_choice={"type": "tool", "name": "meal_suggestions"},
        messages=[{"role": "user", "content": prompt}],
    )
    options = response.content[0].input["options"]
    if len(options) < 2:
        raise ValueError(f"Claude returned {len(options)} meal options — expected 2")
    return options


def get_ingredient_meal(prompt: str) -> dict:
    """Call Claude to generate one meal from the user's available ingredients.

    Uses forced tool use for structured output. Called by nutrition_handlers.py
    after the ingredient collection ConversationHandler completes.

    Args:
        prompt: Fully assembled prompt from nutrition.build_ingredient_meal_prompt().

    Returns:
        Meal dict with: name, kcal, protein_g, fat_g, carbs_g, time_min,
        uses, prep_method, reasoning, tip.
    """
    client = _get_client()
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=800,
        system="You are a precise sports nutritionist. Build the best possible meal from the given ingredients.",
        tools=[_INGREDIENT_MEAL_TOOL],
        tool_choice={"type": "tool", "name": "ingredient_meal"},
        messages=[{"role": "user", "content": prompt}],
    )
    meal = response.content[0].input
    required = {"name", "kcal", "protein_g", "fat_g", "carbs_g", "time_min", "uses", "prep_method", "reasoning"}
    missing = required - meal.keys()
    if missing:
        raise ValueError(f"Claude ingredient meal response missing fields: {missing}")
    return meal
