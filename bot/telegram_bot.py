"""Stage 13 Telegram bot entry point.

Run with:
    python -m bot.telegram_bot

Configuration (environment variables):
    TELEGRAM_BOT_TOKEN                       required, no default. Fails fast if missing.
    PRODUCT_VERIFIER_DB_PATH                 optional, defaults to ./.cache/product_verifier.sqlite3
    PRODUCT_VERIFIER_MAX_CONCURRENT_JOBS     optional, defaults to 2. Must be an integer >= 1.
    PRODUCT_VERIFIER_JOB_HISTORY_LIMIT       optional, defaults to 20. Must be an integer >= 0.

Persistence note: PRODUCT_VERIFIER_DB_PATH is the Stage 11 product-result
cache (SQLite, persistent across restarts). In-flight Telegram jobs
(bot.jobs.JobManager) are in-memory only and are lost on restart -- a user
can simply resend their message; if that product's result is still fresh
in the Stage 11 cache, verification will be fast even though job tracking
was reset.
"""

from __future__ import annotations

import logging
import os
from typing import Mapping

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
from services.product_verifier import ProductVerifierService


TOKEN_ENV_VAR = "TELEGRAM_BOT_TOKEN"
MAX_CONCURRENT_JOBS_ENV_VAR = "PRODUCT_VERIFIER_MAX_CONCURRENT_JOBS"
JOB_HISTORY_LIMIT_ENV_VAR = "PRODUCT_VERIFIER_JOB_HISTORY_LIMIT"
DEFAULT_MAX_CONCURRENT_JOBS = 2
DEFAULT_JOB_HISTORY_LIMIT = 20
SHUTDOWN_TIMEOUT_SECONDS = 30.0

logger = logging.getLogger(__name__)


class MissingBotTokenError(RuntimeError):
    """Raised when TELEGRAM_BOT_TOKEN is not configured."""


class InvalidJobConfigurationError(RuntimeError):
    """Raised for a malformed job-concurrency/history environment value."""


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


def _resolve_int_setting(
    env: Mapping[str, str] | None,
    var_name: str,
    default: int,
    *,
    minimum: int,
) -> int:
    source = env if env is not None else os.environ
    raw = source.get(var_name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip())
    except ValueError as error:
        raise InvalidJobConfigurationError(
            f"{var_name} must be an integer, got {raw!r}"
        ) from error
    if value < minimum:
        raise InvalidJobConfigurationError(
            f"{var_name} must be >= {minimum}, got {value}"
        )
    return value


def resolve_max_concurrent_jobs(env: Mapping[str, str] | None = None) -> int:
    return _resolve_int_setting(
        env, MAX_CONCURRENT_JOBS_ENV_VAR, DEFAULT_MAX_CONCURRENT_JOBS, minimum=1,
    )


def resolve_job_history_limit(env: Mapping[str, str] | None = None) -> int:
    return _resolve_int_setting(
        env, JOB_HISTORY_LIMIT_ENV_VAR, DEFAULT_JOB_HISTORY_LIMIT, minimum=0,
    )


def build_job_manager(
    service: ProductVerifierService,
    *,
    env: Mapping[str, str] | None = None,
) -> JobManager:
    """Build the production JobManager. Fresh instance per call, no singleton."""
    return JobManager(
        service,
        max_concurrent_jobs=resolve_max_concurrent_jobs(env),
        history_limit=resolve_job_history_limit(env),
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
    token = resolve_bot_token()  # fails fast, before any polling starts
    service = build_product_verifier_service()
    manager = build_job_manager(service)
    application = build_application(token, manager)
    logger.info(
        "Starting Telegram bot polling (max_concurrent_jobs=%d, job_history_limit=%d).",
        resolve_max_concurrent_jobs(), resolve_job_history_limit(),
    )
    application.run_polling()


if __name__ == "__main__":
    main()
