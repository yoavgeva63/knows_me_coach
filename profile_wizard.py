"""
Profile setup wizard for the fitness coach bot.

Implements a sequential ConversationHandler that collects (or updates) the 9
required profile fields.  Two entry points:
  - /start  : skips fields that are already filled in DynamoDB
  - /profile: asks every field so the user can update anything

Call build_wizard_handler() to get the configured ConversationHandler to register
with the PTB Application.
"""
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import storage
from auth import is_allowed as _is_allowed

logger = logging.getLogger(__name__)


COMMANDS_HELP = (
    "/morning — daily briefing\n"
    "/clear — reset conversation\n"
    "/remember <fact> — store a note\n"
    "/settime <HH:MM|sleep> — morning alarm\n"
    "/profile — update your profile\n"
    "/connect_garmin — link or relink your Garmin account"
)

# ---------------------------------------------------------------------------
# Wizard states
# ---------------------------------------------------------------------------

(
    WIZARD_NAME,
    WIZARD_AGE,
    WIZARD_WEIGHT,
    WIZARD_GOAL,
    WIZARD_FITNESS_LEVEL,
    WIZARD_WEEKLY_DAYS,
    WIZARD_SESSION_DURATION,
    WIZARD_DIETARY,
    WIZARD_GARMIN,
) = range(9)

# Ordered list of (profile_key, wizard_state) — defines both field order and
# the mapping used by _advance() to skip already-filled fields.
_FIELD_STATES: list[tuple[str, int]] = [
    ("name", WIZARD_NAME),
    ("age", WIZARD_AGE),
    ("weight_kg", WIZARD_WEIGHT),
    ("primary_goal", WIZARD_GOAL),
    ("fitness_level", WIZARD_FITNESS_LEVEL),
    ("weekly_training_days", WIZARD_WEEKLY_DAYS),
    ("preferred_session_duration_minutes", WIZARD_SESSION_DURATION),
    ("dietary_restrictions", WIZARD_DIETARY),
    ("garmin_asked", WIZARD_GARMIN),
]

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _field_is_set(profile: dict, field: str) -> bool:
    """Return True if field has a non-empty value in the profile."""
    val = profile.get(field)
    return val is not None and val != "" and val != []


async def _advance(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    after_field: str | None = None,
) -> int:
    """Send the question for the next unfilled field after after_field, or finish.

    Args:
        after_field: The field just collected. Pass None to start from the beginning.
    Returns the next ConversationHandler state integer.
    """
    profile = context.user_data.get("profile", {})
    skip_filled = context.user_data.get("skip_filled", True)

    found = after_field is None
    for field, state in _FIELD_STATES:
        if not found:
            if field == after_field:
                found = True
            continue
        if skip_filled and _field_is_set(profile, field):
            continue
        return await _ASK_FNS[state](update, context)

    return await _finish_wizard(update, context)


async def _finish_wizard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Persist the collected profile and send the confirmation message."""
    uid = context.user_data["uid"]
    profile = context.user_data["profile"]
    storage.save_profile(uid, profile)
    name = profile.get("name", "there")

    garmin_note = ""
    if context.user_data.get("wants_garmin") and not storage.load_garmin_tokens(uid):
        garmin_note = "\n\nRun /connect_garmin to link your Garmin device and unlock live recovery data in your morning briefing."

    await context.bot.send_message(
        update.effective_chat.id,
        f"All set, {name}! Your profile is saved.{garmin_note}\n\nYou can consult with me as you like.\n\nAvailable commands:\n{COMMANDS_HELP}",
    )
    context.user_data.clear()
    return ConversationHandler.END

# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle /start — greet if profile is complete, else run the setup wizard."""
    user_id = update.effective_user.id
    if not _is_allowed(user_id):
        return ConversationHandler.END

    storage.clear_history(str(user_id))
    profile = storage.load_profile(str(user_id))
    context.user_data["uid"] = str(user_id)
    context.user_data["profile"] = dict(profile)
    context.user_data["skip_filled"] = True  # skip already-set fields during /start

    if all(_field_is_set(profile, f) for f, _ in _FIELD_STATES):
        name = profile.get("name", "there")
        await update.message.reply_text(
            f"Hey {name}! I'm your personal fitness coach.\n\nYou can consult with me as you like.\n\nAvailable commands:\n{COMMANDS_HELP}"
        )
        return ConversationHandler.END

    await update.message.reply_text(
        "Welcome! Let's set up your profile first. You can type /cancel at any time."
    )
    return await _advance(update, context)


async def profile_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle /profile — run the full wizard so the user can update any field."""
    user_id = update.effective_user.id
    if not _is_allowed(user_id):
        return ConversationHandler.END

    profile = storage.load_profile(str(user_id))
    context.user_data["uid"] = str(user_id)
    context.user_data["profile"] = dict(profile)
    context.user_data["skip_filled"] = False  # ask every field so user can update anything

    await update.message.reply_text(
        "Let's update your profile. Type /cancel at any time to stop."
    )
    return await _advance(update, context)


async def wizard_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle /cancel inside the wizard."""
    context.user_data.clear()
    await update.message.reply_text("Profile update cancelled.")
    return ConversationHandler.END

# ---------------------------------------------------------------------------
# Ask functions — send the question for each field
# ---------------------------------------------------------------------------

async def _ask_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Prompt for name and return the WIZARD_NAME state."""
    await context.bot.send_message(update.effective_chat.id, "What's your name?")
    return WIZARD_NAME


async def _ask_age(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Prompt for age and return the WIZARD_AGE state."""
    await context.bot.send_message(update.effective_chat.id, "How old are you?")
    return WIZARD_AGE


async def _ask_weight(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Prompt for weight and return the WIZARD_WEIGHT state."""
    await context.bot.send_message(update.effective_chat.id, "What's your weight in kg?")
    return WIZARD_WEIGHT


async def _ask_goal(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Prompt for primary goal via inline keyboard and return WIZARD_GOAL state."""
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("Cut", callback_data="wiz:goal:Cut"),
        InlineKeyboardButton("Maintain", callback_data="wiz:goal:Maintain"),
        InlineKeyboardButton("Bulk", callback_data="wiz:goal:Bulk"),
    ]])
    await context.bot.send_message(
        update.effective_chat.id, "What's your primary goal?", reply_markup=kb
    )
    return WIZARD_GOAL


async def _ask_fitness_level(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Prompt for fitness level via inline keyboard and return WIZARD_FITNESS_LEVEL state."""
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("Beginner", callback_data="wiz:fitness:Beginner"),
        InlineKeyboardButton("Intermediate", callback_data="wiz:fitness:Intermediate"),
        InlineKeyboardButton("Advanced", callback_data="wiz:fitness:Advanced"),
    ]])
    await context.bot.send_message(
        update.effective_chat.id, "What's your fitness level?", reply_markup=kb
    )
    return WIZARD_FITNESS_LEVEL


async def _ask_weekly_days(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Prompt for weekly training days and return WIZARD_WEEKLY_DAYS state."""
    await context.bot.send_message(
        update.effective_chat.id, "How many days per week can you train? (1–7)"
    )
    return WIZARD_WEEKLY_DAYS


async def _ask_session_duration(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Prompt for session duration and return WIZARD_SESSION_DURATION state."""
    await context.bot.send_message(
        update.effective_chat.id, "How long are your sessions, in minutes? (e.g. 60)"
    )
    return WIZARD_SESSION_DURATION


async def _ask_dietary(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Prompt for dietary restrictions and return WIZARD_DIETARY state."""
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("None", callback_data="wiz:dietary:None"),
    ]])
    await context.bot.send_message(
        update.effective_chat.id,
        "Any dietary restrictions? Type them out, or tap None.",
        reply_markup=kb,
    )
    return WIZARD_DIETARY


async def _ask_garmin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Ask whether the user has a Garmin device and return WIZARD_GARMIN state."""
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("Yes", callback_data="wiz:garmin:yes"),
        InlineKeyboardButton("No", callback_data="wiz:garmin:no"),
    ]])
    await context.bot.send_message(
        update.effective_chat.id,
        "Do you use a Garmin device for activity and sleep tracking?",
        reply_markup=kb,
    )
    return WIZARD_GARMIN


_ASK_FNS: dict[int, any] = {
    WIZARD_NAME: _ask_name,
    WIZARD_AGE: _ask_age,
    WIZARD_WEIGHT: _ask_weight,
    WIZARD_GOAL: _ask_goal,
    WIZARD_FITNESS_LEVEL: _ask_fitness_level,
    WIZARD_WEEKLY_DAYS: _ask_weekly_days,
    WIZARD_SESSION_DURATION: _ask_session_duration,
    WIZARD_DIETARY: _ask_dietary,
    WIZARD_GARMIN: _ask_garmin,
}

# ---------------------------------------------------------------------------
# Step handlers — receive the user's answer and advance
# ---------------------------------------------------------------------------

async def wizard_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Collect name and advance."""
    context.user_data["profile"]["name"] = update.message.text.strip()
    return await _advance(update, context, after_field="name")


async def wizard_age(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Validate and collect age, then advance."""
    text = update.message.text.strip()
    if not text.isdigit() or not (8 <= int(text) <= 100):
        await update.message.reply_text("Please enter a valid age between 8 and 100.")
        return WIZARD_AGE
    context.user_data["profile"]["age"] = int(text)
    return await _advance(update, context, after_field="age")


async def wizard_weight(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Validate and collect weight (kg), then advance."""
    try:
        w = float(update.message.text.strip())
        if not (20 <= w <= 300):
            raise ValueError
    except ValueError:
        await update.message.reply_text("Please enter a valid weight in kg (20–300).")
        return WIZARD_WEIGHT
    context.user_data["profile"]["weight_kg"] = w
    return await _advance(update, context, after_field="weight_kg")


async def wizard_weekly_days(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Validate and collect weekly training days, then advance."""
    text = update.message.text.strip()
    if not text.isdigit() or not (1 <= int(text) <= 7):
        await update.message.reply_text("Please enter a number between 1 and 7.")
        return WIZARD_WEEKLY_DAYS
    context.user_data["profile"]["weekly_training_days"] = int(text)
    return await _advance(update, context, after_field="weekly_training_days")


async def wizard_session_duration(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Validate and collect session duration (minutes), then advance."""
    text = update.message.text.strip()
    if not text.isdigit() or not (10 <= int(text) <= 300):
        await update.message.reply_text("Please enter a duration in minutes (10–300).")
        return WIZARD_SESSION_DURATION
    context.user_data["profile"]["preferred_session_duration_minutes"] = int(text)
    return await _advance(update, context, after_field="preferred_session_duration_minutes")


async def wizard_dietary_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Collect free-text dietary restrictions, then advance."""
    context.user_data["profile"]["dietary_restrictions"] = update.message.text.strip()
    return await _advance(update, context, after_field="dietary_restrictions")


async def wizard_goal_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle goal selection button (Cut / Maintain / Bulk)."""
    query = update.callback_query
    await query.answer()
    goal = query.data.split(":")[2]  # "wiz:goal:Cut" → "Cut"
    context.user_data["profile"]["primary_goal"] = goal
    return await _advance(update, context, after_field="primary_goal")


async def wizard_fitness_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle fitness level selection button."""
    query = update.callback_query
    await query.answer()
    level = query.data.split(":")[2]  # "wiz:fitness:Intermediate" → "Intermediate"
    context.user_data["profile"]["fitness_level"] = level
    return await _advance(update, context, after_field="fitness_level")


async def wizard_dietary_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle the 'None' dietary restrictions button."""
    query = update.callback_query
    await query.answer()
    context.user_data["profile"]["dietary_restrictions"] = "None"
    return await _advance(update, context, after_field="dietary_restrictions")


async def wizard_garmin_yes_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle the 'Yes' Garmin button — mark asked and flag intent to connect."""
    query = update.callback_query
    await query.answer()
    context.user_data["profile"]["garmin_asked"] = True
    context.user_data["wants_garmin"] = True
    return await _advance(update, context, after_field="garmin_asked")


async def wizard_garmin_no_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle the 'No' Garmin button — mark asked and skip Garmin setup."""
    query = update.callback_query
    await query.answer()
    context.user_data["profile"]["garmin_asked"] = True
    context.user_data["wants_garmin"] = False
    return await _advance(update, context, after_field="garmin_asked")

# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------

def build_wizard_handler() -> ConversationHandler:
    """Return the configured ConversationHandler for /start and /profile."""
    return ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            CommandHandler("profile", profile_cmd),
        ],
        states={
            WIZARD_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, wizard_name)],
            WIZARD_AGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, wizard_age)],
            WIZARD_WEIGHT: [MessageHandler(filters.TEXT & ~filters.COMMAND, wizard_weight)],
            WIZARD_GOAL: [CallbackQueryHandler(wizard_goal_cb, pattern=r"^wiz:goal:")],
            WIZARD_FITNESS_LEVEL: [CallbackQueryHandler(wizard_fitness_cb, pattern=r"^wiz:fitness:")],
            WIZARD_WEEKLY_DAYS: [MessageHandler(filters.TEXT & ~filters.COMMAND, wizard_weekly_days)],
            WIZARD_SESSION_DURATION: [MessageHandler(filters.TEXT & ~filters.COMMAND, wizard_session_duration)],
            WIZARD_DIETARY: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, wizard_dietary_text),
                CallbackQueryHandler(wizard_dietary_cb, pattern=r"^wiz:dietary:"),
            ],
            WIZARD_GARMIN: [
                CallbackQueryHandler(wizard_garmin_yes_cb, pattern=r"^wiz:garmin:yes$"),
                CallbackQueryHandler(wizard_garmin_no_cb, pattern=r"^wiz:garmin:no$"),
            ],
        },
        fallbacks=[CommandHandler("cancel", wizard_cancel)],
    )
