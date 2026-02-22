"""
AWS Lambda entry point for the Telegram fitness coach bot.

API Gateway receives Telegram webhook POST → triggers this Lambda.
Secrets are loaded from AWS Secrets Manager (set env var USE_SECRETS_MANAGER=true)
or from Lambda environment variables directly.

Deploy steps (high level):
1. pip install -r requirements.txt -t package/
2. Zip package/ + this file + brain.py
3. Upload to Lambda, set handler to lambda_handler.lambda_handler
4. Set API Gateway POST route → Lambda
5. Register webhook: https://api.telegram.org/bot<TOKEN>/setWebhook?url=<API_GW_URL>
"""
import json
import logging
import os
from collections import defaultdict

from telegram import Update, Bot
from telegram.ext import Application, CommandHandler, MessageHandler, filters

from brain import get_claude_response

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Simple in-memory store — replace with DynamoDB in the next iteration
conversation_histories: dict[int, list[dict]] = defaultdict(list)

ALLOWED_USER_ID = int(os.environ.get("ALLOWED_TELEGRAM_USER_ID", 0))


def is_allowed(user_id: int) -> bool:
    if ALLOWED_USER_ID == 0:
        return True
    return user_id == ALLOWED_USER_ID


async def _process_update(update: Update, bot: Bot) -> None:
    """Core update processing logic shared by both local and Lambda modes."""
    if not update.message or not update.message.text:
        return

    user_id = update.effective_user.id
    if not is_allowed(user_id):
        logger.warning("Blocked unauthorized user %s", user_id)
        return

    text = update.message.text.strip()
    chat_id = update.effective_chat.id

    if text == "/start":
        conversation_histories[user_id].clear()
        await bot.send_message(
            chat_id,
            "Hey! I'm your personal fitness coach 💪\n\n"
            "Tell me about your goals, ask for a workout, log a meal, or just chat!",
        )
        return

    if text == "/clear":
        conversation_histories[user_id].clear()
        await bot.send_message(chat_id, "Conversation cleared. Fresh start! 🔄")
        return

    history = conversation_histories[user_id]
    try:
        reply = get_claude_response(history, text)
    except Exception as exc:
        logger.error("Claude error: %s", exc)
        await bot.send_message(chat_id, "Sorry, I had a hiccup. Try again in a moment.")
        return

    history.append({"role": "user", "content": text})
    history.append({"role": "assistant", "content": reply})

    if len(history) > 40:
        conversation_histories[user_id] = history[-40:]

    await bot.send_message(chat_id, reply)


def lambda_handler(event: dict, context) -> dict:
    """AWS Lambda entry point."""
    import asyncio

    logger.info("Received event: %s", json.dumps(event)[:500])

    token = os.environ["TELEGRAM_BOT_TOKEN"]
    bot = Bot(token=token)

    try:
        body = json.loads(event.get("body", "{}"))
        update = Update.de_json(body, bot)
        asyncio.get_event_loop().run_until_complete(_process_update(update, bot))
    except Exception as exc:
        logger.error("Unhandled error: %s", exc, exc_info=True)

    # Always return 200 so Telegram doesn't retry
    return {"statusCode": 200, "body": "ok"}
