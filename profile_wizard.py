"""
Profile setup wizard for the fitness coach bot.

Implements a sequential ConversationHandler that collects (or updates) the
required profile fields, then guides the user through Garmin Connect setup
and morning alarm configuration.

Two entry points:
  - /start  : skips fields that are already filled in DynamoDB
  - /profile: asks every field so the user can update anything

Call build_wizard_handler() to get the configured ConversationHandler to register
with the PTB Application.
"""
import logging
import re

from garminconnect import GarminConnectAuthenticationError
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import garmin
import storage
from auth import is_allowed as _is_allowed

logger = logging.getLogger(__name__)


COMMANDS_HELP = (
    "Just talk to me naturally — here are some things you can say:\n"
    "• \"Give me my morning briefing\" — get today's workout plan\n"
    "• \"Set my alarm to 07:30\" — change your morning briefing time\n"
    "• \"Remember that I have a sore knee\" — save something important\n"
    "• \"Clear our conversation\" — start fresh\n"
    "• \"Update my profile\" — run /profile to change your goals, weight, etc.\n"
    "• \"Connect my Garmin\" — run /connect_garmin to relink your device"
)

# ---------------------------------------------------------------------------
# Wizard states
# ---------------------------------------------------------------------------

(
    WIZARD_NAME,
    WIZARD_AGE,
    WIZARD_WEIGHT,
    WIZARD_HEIGHT,
    WIZARD_SEX,
    WIZARD_GOAL,
    WIZARD_FITNESS_LEVEL,
    WIZARD_WEEKLY_DAYS,
    WIZARD_SESSION_DURATION,
    WIZARD_DIETARY,
    WIZARD_GARMIN,
    WIZARD_GARMIN_EMAIL,
    WIZARD_GARMIN_PASSWORD,
    WIZARD_ALARM_TIME,
) = range(14)

# Ordered list of (profile_key, wizard_state) — defines both field order and
# the mapping used by _advance() to skip already-filled fields.
_FIELD_STATES: list[tuple[str, int]] = [
    ("name", WIZARD_NAME),
    ("age", WIZARD_AGE),
    ("weight_kg", WIZARD_WEIGHT),
    ("height_cm", WIZARD_HEIGHT),
    ("sex", WIZARD_SEX),
    ("primary_goal", WIZARD_GOAL),
    ("fitness_level", WIZARD_FITNESS_LEVEL),
    ("weekly_training_days", WIZARD_WEEKLY_DAYS),
    ("preferred_session_duration_minutes", WIZARD_SESSION_DURATION),
    ("dietary_restrictions", WIZARD_DIETARY),
    ("garmin_asked", WIZARD_GARMIN),
    ("morning_alarm_time", WIZARD_ALARM_TIME),
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
        garmin_note = "\n\n⚠️ Garmin linking failed — run /connect_garmin to try again."

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


async def _ask_height(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Prompt for height and return the WIZARD_HEIGHT state."""
    await context.bot.send_message(update.effective_chat.id, "What's your height in cm? (e.g. 178)")
    return WIZARD_HEIGHT


async def _ask_sex(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Prompt for biological sex via inline keyboard and return WIZARD_SEX state."""
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("Male", callback_data="wiz:sex:male"),
        InlineKeyboardButton("Female", callback_data="wiz:sex:female"),
    ]])
    await context.bot.send_message(
        update.effective_chat.id, "What's your biological sex? (used for calorie calculation)", reply_markup=kb
    )
    return WIZARD_SEX


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


async def _ask_garmin_email(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Prompt for Garmin Connect email and return WIZARD_GARMIN_EMAIL state."""
    await context.bot.send_message(
        update.effective_chat.id,
        "What's your Garmin Connect email address?",
    )
    return WIZARD_GARMIN_EMAIL


async def _ask_alarm_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Ask when to send the morning briefing and return WIZARD_ALARM_TIME state."""
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("07:00", callback_data="wiz:alarm:07:00"),
            InlineKeyboardButton("08:00", callback_data="wiz:alarm:08:00"),
            InlineKeyboardButton("09:00", callback_data="wiz:alarm:09:00"),
        ],
        [
            InlineKeyboardButton("🌙 Sleep mode (auto-detect)", callback_data="wiz:alarm:sleep"),
        ],
    ])
    await context.bot.send_message(
        update.effective_chat.id,
        "When should I send your morning briefing?\n"
        "Pick a time or choose Sleep mode — I'll trigger it automatically "
        "when Garmin detects you've woken up.\n"
        "You can also type a custom time (e.g. 08:45).",
        reply_markup=kb,
    )
    return WIZARD_ALARM_TIME


_ASK_FNS: dict[int, any] = {
    WIZARD_NAME: _ask_name,
    WIZARD_AGE: _ask_age,
    WIZARD_WEIGHT: _ask_weight,
    WIZARD_HEIGHT: _ask_height,
    WIZARD_SEX: _ask_sex,
    WIZARD_GOAL: _ask_goal,
    WIZARD_FITNESS_LEVEL: _ask_fitness_level,
    WIZARD_WEEKLY_DAYS: _ask_weekly_days,
    WIZARD_SESSION_DURATION: _ask_session_duration,
    WIZARD_DIETARY: _ask_dietary,
    WIZARD_GARMIN: _ask_garmin,
    WIZARD_ALARM_TIME: _ask_alarm_time,
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


async def wizard_height(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Validate and collect height (cm), then advance."""
    try:
        h = int(update.message.text.strip())
        if not (100 <= h <= 220):
            raise ValueError
    except ValueError:
        await update.message.reply_text("Please enter a valid height in cm (100–220).")
        return WIZARD_HEIGHT
    context.user_data["profile"]["height_cm"] = h
    return await _advance(update, context, after_field="height_cm")


async def wizard_sex_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle biological sex selection button (Male / Female)."""
    query = update.callback_query
    await query.answer()
    sex = query.data.split(":")[2]  # "wiz:sex:male" → "male"
    context.user_data["profile"]["sex"] = sex
    return await _advance(update, context, after_field="sex")


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
    """Handle the 'Yes' Garmin button — mark asked and start the Garmin connect flow."""
    query = update.callback_query
    await query.answer()
    context.user_data["profile"]["garmin_asked"] = True
    context.user_data["wants_garmin"] = True
    return await _ask_garmin_email(update, context)


async def wizard_garmin_no_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle the 'No' Garmin button — mark asked and go straight to alarm time."""
    query = update.callback_query
    await query.answer()
    context.user_data["profile"]["garmin_asked"] = True
    context.user_data["wants_garmin"] = False
    return await _advance(update, context, after_field="garmin_asked")


async def wizard_garmin_email(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Collect Garmin email and ask for password."""
    context.user_data["garmin_email"] = update.message.text.strip()
    await context.bot.send_message(
        update.effective_chat.id,
        "Now enter your Garmin Connect password.\n"
        "⚠️ Your message will be visible in chat — delete it after sending.",
    )
    return WIZARD_GARMIN_PASSWORD


async def wizard_garmin_password(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Collect password, authenticate with Garmin, then proceed to alarm time."""
    uid = context.user_data["uid"]
    email = context.user_data.pop("garmin_email", "")
    password = update.message.text.strip()

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    try:
        garmin.initial_login(uid, email, password)
        await update.message.reply_text("✅ Garmin Connect linked!")
    except GarminConnectAuthenticationError:
        await update.message.reply_text(
            "❌ Authentication failed — you can try again later with /connect_garmin."
        )
    except Exception as exc:
        logger.error("Garmin connect error for %s: %s", uid, exc)
        await update.message.reply_text(
            "Something went wrong connecting to Garmin. You can try again later with /connect_garmin."
        )

    return await _ask_alarm_time(update, context)


async def wizard_alarm_time_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle alarm time button selection."""
    query = update.callback_query
    await query.answer()
    alarm = query.data.split(":", 2)[2]  # "wiz:alarm:07:30" → "07:30", "wiz:alarm:sleep" → "sleep"
    context.user_data["profile"]["morning_alarm_time"] = alarm
    return await _advance(update, context, after_field="morning_alarm_time")


async def wizard_alarm_time_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle free-text alarm time entry (HH:MM or 'sleep')."""
    text = update.message.text.strip().lower()
    if text == "sleep":
        alarm = "sleep"
    elif re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", text):
        alarm = text
    else:
        await update.message.reply_text("Please enter a valid time like 06:45, or 'sleep'.")
        return WIZARD_ALARM_TIME
    context.user_data["profile"]["morning_alarm_time"] = alarm
    return await _advance(update, context, after_field="morning_alarm_time")

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
            WIZARD_HEIGHT: [MessageHandler(filters.TEXT & ~filters.COMMAND, wizard_height)],
            WIZARD_SEX: [CallbackQueryHandler(wizard_sex_cb, pattern=r"^wiz:sex:")],
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
            WIZARD_GARMIN_EMAIL: [MessageHandler(filters.TEXT & ~filters.COMMAND, wizard_garmin_email)],
            WIZARD_GARMIN_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, wizard_garmin_password)],
            WIZARD_ALARM_TIME: [
                CallbackQueryHandler(wizard_alarm_time_cb, pattern=r"^wiz:alarm:"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, wizard_alarm_time_text),
            ],
        },
        fallbacks=[CommandHandler("cancel", wizard_cancel)],
    )
