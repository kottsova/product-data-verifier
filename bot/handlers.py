"""Telegram-facing handlers.

Stage 13: product verification runs as a background job (bot.jobs.JobManager)
instead of blocking the handler until service.verify() returns. The handler
submits a job, replies immediately (accepted/duplicate), and the job's
on_update callback -- wired to the same ``reply`` here -- delivers further
updates (started / completed / failed) asynchronously. A cancelled job
delivers nothing further (see bot.jobs / bot.formatters.format_job_outcome).

Parsing (bot.parser) and result formatting (bot.formatters) remain pure,
framework-free functions from Stage 12. This module still never imports
core.workflow, core.profile, or core.quality -- only bot.jobs and
services.product_verifier.
"""

from __future__ import annotations

import logging
from typing import Awaitable, Callable

from bot.formatters import (
    format_accepted,
    format_cancelled,
    format_duplicate,
    format_job_outcome,
    format_started,
    format_status,
)
from bot.jobs import Job, JobManager, JobManagerShuttingDownError
from bot.parser import parse_product_query
from services.product_verifier import VerifyProductRequest


logger = logging.getLogger(__name__)

Reply = Callable[[str], Awaitable[object]]

START_MESSAGE = (
    "Привет! Я проверяю характеристики товара по бренду и модели.\n\n"
    "Отправьте сообщение в формате:\n"
    "Bosch PUE611BB5E\n"
    "или\n"
    "Bosch | PUE611BB5E\n\n"
    "Проверка выполняется в фоне; я пришлю результат, когда он будет готов.\n"
    "Команда /help покажет подробности."
)

HELP_MESSAGE = (
    "Формат запроса:\n"
    "<бренд> <модель>\n"
    "или\n"
    "<бренд> | <модель> | <артикул (необязательно)>\n\n"
    "Примеры:\n"
    "Bosch PUE611BB5E\n"
    "HONOR | X8d\n"
    "Dreame | G12 Pro | HHR32A\n\n"
    "Проверка товара выполняется в фоне и может занять несколько минут; "
    "я пришлю сообщение, когда результат будет готов.\n\n"
    "Команды:\n"
    "/status — показать ваши текущие (queued/running) задачи\n"
    "/cancel — отменить ваши активные задачи"
)

PARSE_ERROR_MESSAGE = (
    "Не удалось понять запрос. Отправьте бренд и модель, например:\n"
    "Bosch PUE611BB5E\n"
    "или Bosch | PUE611BB5E"
)

SHUTTING_DOWN_MESSAGE = "Бот перезапускается, попробуйте отправить запрос через минуту."


async def handle_product_query(
    text: str,
    chat_id: int,
    manager: JobManager,
    *,
    reply: Reply,
) -> None:
    """Parse the message, submit a background job, and reply immediately.

    Further updates (started / final result) arrive later through the job's
    on_update callback, wired to this same ``reply``.
    """
    query = parse_product_query(text)
    if query is None:
        await reply(PARSE_ERROR_MESSAGE)
        return

    request = VerifyProductRequest(brand=query.brand, model=query.model, article=query.article)

    async def on_update(job: Job) -> None:
        if job.state == "running":
            await reply(format_started(job.request))
            return
        for chunk in format_job_outcome(job):
            await reply(chunk)

    try:
        job, is_duplicate = await manager.submit(chat_id, request, on_update=on_update)
    except JobManagerShuttingDownError:
        await reply(SHUTTING_DOWN_MESSAGE)
        return

    if is_duplicate:
        await reply(format_duplicate(job))
        return
    await reply(format_accepted(job.request))


async def _safe_reply(message: object, text: str) -> None:
    """Adapter-level guard: a failed Telegram send must not crash the bot."""
    try:
        await message.reply_text(text)  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - Telegram/API send failures are logged, not raised
        logger.exception("Failed to send a Telegram reply")


def _chat_id(update) -> int:  # noqa: ANN001 - telegram.Update, kept duck-typed for testability
    return update.message.chat_id


async def start_command(update, context) -> None:  # noqa: ANN001 - telegram.ext handler signature
    await _safe_reply(update.message, START_MESSAGE)


async def help_command(update, context) -> None:  # noqa: ANN001 - telegram.ext handler signature
    await _safe_reply(update.message, HELP_MESSAGE)


def build_verify_command(manager: JobManager):
    """Bind a verify handler to ``manager`` via closure -- no global state."""

    async def verify_command(update, context) -> None:  # noqa: ANN001
        text = update.message.text or ""
        chat_id = _chat_id(update)

        async def reply(chunk: str) -> None:
            await _safe_reply(update.message, chunk)

        await handle_product_query(text, chat_id, manager, reply=reply)

    return verify_command


def build_status_command(manager: JobManager):
    async def status_command(update, context) -> None:  # noqa: ANN001
        jobs = manager.active_jobs_for_chat(_chat_id(update))
        await _safe_reply(update.message, format_status(jobs))

    return status_command


def build_cancel_command(manager: JobManager):
    async def cancel_command(update, context) -> None:  # noqa: ANN001
        count = await manager.cancel_chat_jobs(_chat_id(update))
        await _safe_reply(update.message, format_cancelled(count))

    return cancel_command
