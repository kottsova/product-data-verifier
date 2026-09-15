"""Stage 14 Telegram bot entry point.

Run with:
    python -m bot.telegram_bot

Configuration is centralized in ``config.py`` (``AppConfig`` /
``TelegramConfig``) -- this module no longer reads the environment itself.
See the README for the full environment variable table.

Persistence note: PRODUCT_VERIFIER_DB_PATH is the Stage 11 product-result
cache (SQLite, persistent across restarts). In-flight Telegram jobs
(bot.jobs.JobManager) are in-memory only and are lost on restart -- a user
can simply resend their message; if that product's result is still fresh
in the Stage 11 cache, verification will be fast even though job tracking
was reset.
"""

from __future__ import annotations

import logging

from telegram.ext import Application, CommandHandler, MessageHandler, filters

from bot.handlers import (
    build_cancel_command,
    build_status_command,
    build_verify_command,
    help_command,
    start_command,
)
from bot.jobs import JobManager
from bot.service_factory import build_product_verifier_service
from config import AppConfig, ConfigurationError, TelegramConfig
from services.product_verifier import ProductVerifierService


SHUTDOWN_TIMEOUT_SECONDS = 30.0

logger = logging.getLogger(__name__)


def build_job_manager(service: ProductVerifierService, config: AppConfig) -> JobManager:
    """Build the production JobManager. Fresh instance per call, no singleton."""
    return JobManager(
        service,
        max_concurrent_jobs=config.max_concurrent_jobs,
        history_limit=config.job_history_limit,
    )


def build_application(token: str, manager: JobManager) -> Application:
    """Wire the job manager into a python-telegram-bot Application.

    Only bot.jobs / services.product_verifier are used here -- no
    core.workflow, core.profile, or core.quality import anywhere in the bot
    layer.
    """

    async def _shutdown(_application: Application) -> None:
        await manager.shutdown(timeout=SHUTDOWN_TIMEOUT_SECONDS)

    application = Application.builder().token(token).post_shutdown(_shutdown).build()
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("status", build_status_command(manager)))
    application.add_handler(CommandHandler("cancel", build_cancel_command(manager)))
    application.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND, build_verify_command(manager),
    ))
    return application


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    try:
        app_config = AppConfig.from_env()
        telegram_config = TelegramConfig.from_env()  # fails fast, before any polling/DB I/O
    except ConfigurationError:
        logger.exception("Configuration error; refusing to start the bot.")
        raise SystemExit(1)

    service = build_product_verifier_service(app_config)
    manager = build_job_manager(service, app_config)
    application = build_application(telegram_config.bot_token, manager)
    logger.info(
        "Starting Telegram bot polling (max_concurrent_jobs=%d, job_history_limit=%d, "
        "cache_ttl_seconds=%s).",
        app_config.max_concurrent_jobs, app_config.job_history_limit, app_config.cache_ttl_seconds,
    )
    application.run_polling()


if __name__ == "__main__":
    main()
