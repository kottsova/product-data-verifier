"""Stage 16 deployment-ready Telegram bot entry point.

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
import signal

from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, filters

from bot.handlers import (
    build_cancel_command,
    build_discovery_command,
    build_discovery_text_handler,
    build_export_command,
    build_language_callback,
    build_photos_callback,
    build_status_command,
    build_verify_command,
    help_command,
    start_command,
)
from bot.jobs import JobManager
from bot.service_factory import build_product_verifier_service
from config import AppConfig, ConfigurationError, TelegramConfig
from observability import configure_logging, log_event, log_exception_event
from services.product_verifier import ProductVerifierService
from services.discovery_debug import DiscoveryDebugService


SHUTDOWN_TIMEOUT_SECONDS = 30.0
STOP_SIGNALS = (signal.SIGINT, signal.SIGTERM)

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())


def build_job_manager(service: ProductVerifierService, config: AppConfig) -> JobManager:
    """Build the production JobManager. Fresh instance per call, no singleton."""
    return JobManager(
        service,
        max_concurrent_jobs=config.max_concurrent_jobs,
        history_limit=config.job_history_limit,
        metrics=getattr(service, "metrics", None),
    )


def build_application(
    token: str,
    manager: JobManager | None,
    discovery_service: DiscoveryDebugService | None = None,
    *,
    discovery_only: bool = False,
) -> Application:
    """Wire the job manager into a python-telegram-bot Application.

    Only bot.jobs / services.product_verifier are used here -- no
    core.workflow, core.profile, or core.quality import anywhere in the bot
    layer.
    """

    async def _started(_application: Application) -> None:
        log_event(logger, logging.INFO, "bot_started")

    async def _shutdown(_application: Application) -> None:
        try:
            if manager is not None:
                await manager.shutdown(timeout=SHUTDOWN_TIMEOUT_SECONDS)
        finally:
            log_event(logger, logging.INFO, "bot_shutdown")

    application = (
        Application.builder().token(token).post_init(_started).post_shutdown(_shutdown).build()
    )
    if discovery_only:
        if discovery_service is None:
            raise ValueError("discovery-only mode requires a discovery service")

        async def discovery_intro(update, context) -> None:  # noqa: ANN001
            await update.message.reply_text(
                "Stage 33.0: отправьте название товара без URL. "
                "/discover_rejected покажет все отклонённые ссылки последнего запроса."
            )

        application.add_handler(CommandHandler("start", discovery_intro))
        application.add_handler(CommandHandler("help", discovery_intro))
        application.add_handler(CommandHandler("discover", build_discovery_command(discovery_service)))
        application.add_handler(CommandHandler(
            "discover_rejected", build_discovery_command(discovery_service, include_all_rejected=True),
        ))
        application.add_handler(MessageHandler(
            filters.TEXT & ~filters.COMMAND, build_discovery_text_handler(discovery_service),
        ))
        return application

    if manager is None:
        raise ValueError("verification mode requires a job manager")
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("status", build_status_command(manager)))
    application.add_handler(CommandHandler("cancel", build_cancel_command(manager)))
    application.add_handler(CommandHandler("export", build_export_command(manager)))
    if discovery_service is not None:
        application.add_handler(CommandHandler(
            "discover", build_discovery_command(discovery_service),
        ))
        application.add_handler(CommandHandler(
            "discover_rejected",
            build_discovery_command(discovery_service, include_all_rejected=True),
        ))
    application.add_handler(CallbackQueryHandler(build_language_callback(manager), pattern=r"^lang:"))
    application.add_handler(CallbackQueryHandler(build_photos_callback(manager), pattern=r"^photos:"))
    application.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND,
        build_verify_command(manager),
    ))
    return application


def validate_runtime_wiring(manager: JobManager) -> None:
    """Fail before polling when persistence or diagnostics are unavailable."""
    snapshot = manager.diagnostics().to_dict()
    repository = snapshot.get("repository", {})
    if not isinstance(repository, dict):
        raise RuntimeError("invalid repository diagnostics")
    if repository.get("configured") is not True:
        raise RuntimeError("production repository is not configured")
    if repository.get("available") is not True:
        raise RuntimeError("configured repository is unavailable")


def main() -> None:
    try:
        app_config = AppConfig.from_env()
    except ConfigurationError as error:
        configure_logging(AppConfig())
        log_event(
            logger, logging.ERROR, "bot_configuration_failure",
            reason="invalid_application_config", error_type=type(error).__name__,
        )
        raise SystemExit(1)

    configure_logging(app_config)
    log_event(logger, logging.INFO, "bot_starting")
    try:
        telegram_config = TelegramConfig.from_env()  # before polling/DB I/O
    except ConfigurationError as error:
        log_event(
            logger, logging.ERROR, "bot_configuration_failure",
            reason="missing_or_invalid_telegram_token", error_type=type(error).__name__,
        )
        raise SystemExit(1)

    # Reconfigure once the secret is known so every normal and exception log
    # path redacts it. The token itself is never emitted as a field.
    configure_logging(app_config, secrets=(telegram_config.bot_token,))

    try:
        if app_config.discovery_only:
            application = build_application(
                telegram_config.bot_token, None, DiscoveryDebugService(),
                discovery_only=True,
            )
        else:
            service = build_product_verifier_service(app_config)
            manager = build_job_manager(service, app_config)
            validate_runtime_wiring(manager)
            application = build_application(
                telegram_config.bot_token, manager, DiscoveryDebugService(),
            )
    except Exception as error:  # noqa: BLE001 - production startup must fail cleanly
        log_exception_event(
            logger, "bot_startup_failure",
            phase="runtime_wiring", error_type=type(error).__name__,
        )
        raise SystemExit(1)
    log_event(
        logger, logging.INFO, "bot_polling",
        max_concurrent_jobs=app_config.max_concurrent_jobs,
        job_history_limit=app_config.job_history_limit,
        cache_ttl_seconds=app_config.cache_ttl_seconds,
    )
    application.run_polling(stop_signals=STOP_SIGNALS)


if __name__ == "__main__":
    main()
