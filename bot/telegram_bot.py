"""Stage 12 Telegram bot entry point.

Run with:
    python -m bot.telegram_bot

Configuration (environment variables):
    TELEGRAM_BOT_TOKEN      required, no default. Fails fast if missing.
    PRODUCT_VERIFIER_DB_PATH  optional, defaults to ./.cache/product_verifier.sqlite3
"""

from __future__ import annotations

import logging
import os
from typing import Mapping

from telegram.ext import Application, CommandHandler, MessageHandler, filters

from bot.handlers import build_verify_command, help_command, start_command
from bot.service_factory import build_product_verifier_service
from services.product_verifier import ProductVerifierService


TOKEN_ENV_VAR = "TELEGRAM_BOT_TOKEN"

logger = logging.getLogger(__name__)


class MissingBotTokenError(RuntimeError):
    """Raised when TELEGRAM_BOT_TOKEN is not configured."""


def resolve_bot_token(env: Mapping[str, str] | None = None) -> str:
    """Read the bot token from the environment. Never hardcoded, never logged."""
    source = env if env is not None else os.environ
    token = (source.get(TOKEN_ENV_VAR) or "").strip()
    if not token:
        raise MissingBotTokenError(
            f"{TOKEN_ENV_VAR} is not set. Set it before starting the bot, e.g.:\n"
            f'  {TOKEN_ENV_VAR}="123456:ABC..." python -m bot.telegram_bot'
        )
    return token


def build_application(token: str, service: ProductVerifierService) -> Application:
    """Wire the stable service boundary into a python-telegram-bot Application.

    Only ``services.product_verifier`` is used here -- no core.workflow,
    core.profile, or core.quality import anywhere in the bot layer.
    """
    application = Application.builder().token(token).build()
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND, build_verify_command(service),
    ))
    return application


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    token = resolve_bot_token()  # fails fast, before any polling starts
    service = build_product_verifier_service()
    application = build_application(token, service)
    logger.info("Starting Telegram bot polling.")
    application.run_polling()


if __name__ == "__main__":
    main()
