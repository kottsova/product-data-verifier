"""Telegram-facing handlers.

Stage 13: product verification runs as a background job (bot.jobs.JobManager)
instead of blocking the handler until service.verify() returns. The handler
submits a job, replies immediately (accepted/duplicate), and the job's
on_update callback -- wired to the same ``reply`` here -- delivers further
updates (started / completed / failed) asynchronously. A cancelled job
delivers nothing further (see bot.jobs / bot.formatters.format_job_outcome).

Stage 31.1: before a job is submitted, the chat is asked to pick a result
language (RU/EN, see bot.i18n) via an inline keyboard. handle_product_query
only parses the message and stores the pending request (bot.jobs.JobManager.
set_pending_query); build_language_callback's CallbackQueryHandler resolves
the button tap and actually starts the job, reusing _start_verification.

Parsing (bot.parser) and result formatting (bot.formatters) remain pure,
framework-free functions from Stage 12. This module still never imports
core.workflow, core.profile, or core.quality -- only bot.jobs and
services.product_verifier.
"""

from __future__ import annotations

import logging
import re
from typing import Awaitable, Callable

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from bot.export import export_result_csv
from bot.formatters import (
    DEFAULT_MAX_MESSAGE_LENGTH,
    chunk_lines,
    format_accepted,
    format_cancelled,
    format_duplicate,
    format_job_outcome,
    format_started,
    format_status,
)
from bot.i18n import (
    DEFAULT_LANGUAGE,
    LANGUAGE_CHOICE_LABELS,
    Language,
    language_prompt,
    normalize_language,
    ui,
)
from bot.jobs import Job, JobManager, JobManagerShuttingDownError
from bot.parser import parse_product_query
from observability import log_event, log_exception_event, scoped_id
from services.product_verifier import VerifyProductRequest


logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

Reply = Callable[[str], Awaitable[object]]
PromptLanguage = Callable[[str, list[tuple[str, str]]], Awaitable[object]]

START_MESSAGE = ui("start_message", DEFAULT_LANGUAGE)
HELP_MESSAGE = ui("help_message", DEFAULT_LANGUAGE)
PARSE_ERROR_MESSAGE = ui("parse_error", DEFAULT_LANGUAGE)
SHUTTING_DOWN_MESSAGE = ui("shutting_down", DEFAULT_LANGUAGE)
EXPORT_NO_RESULT_MESSAGE = ui("export_no_result", DEFAULT_LANGUAGE)

_LANGUAGE_CALLBACK_PREFIX = "lang:"

_EXPORT_FILENAME_SAFE_PATTERN = re.compile(r"[^0-9A-Za-zА-Яа-яЁё]+")


async def handle_product_query(
    text: str,
    chat_id: int,
    manager: JobManager,
    *,
    reply: Reply,
    prompt_language: PromptLanguage,
) -> None:
    """Parse the message and ask which language to show the result in.

    The job itself is not submitted here -- see _start_verification, called
    once the chat answers the language prompt (build_language_callback).
    """
    query = parse_product_query(text)
    if query is None:
        log_event(
            logger, logging.INFO, "request_rejected",
            chat=scoped_id(chat_id), reason="invalid_product_query",
        )
        await reply(PARSE_ERROR_MESSAGE)
        return

    request = VerifyProductRequest(brand=query.brand, model=query.model, article=query.article)
    manager.set_pending_query(chat_id, request)
    buttons = [
        (LANGUAGE_CHOICE_LABELS["ru"], f"{_LANGUAGE_CALLBACK_PREFIX}ru"),
        (LANGUAGE_CHOICE_LABELS["en"], f"{_LANGUAGE_CALLBACK_PREFIX}en"),
    ]
    log_event(
        logger, logging.INFO, "language_prompt_sent",
        chat=scoped_id(chat_id),
    )
    await prompt_language(language_prompt(), buttons)


async def _start_verification(
    request: VerifyProductRequest,
    chat_id: int,
    manager: JobManager,
    *,
    language: Language,
    reply: Reply,
) -> None:
    """Submit the job and reply immediately (accepted/duplicate); Stage 13 flow."""

    async def on_update(job: Job) -> None:
        if job.state == "running":
            await reply(format_started(job.request, language=language))
            return
        for chunk in format_job_outcome(job, language=language):
            await reply(chunk)

    try:
        job, is_duplicate = await manager.submit(
            chat_id, request, language=language, on_update=on_update,
        )
    except JobManagerShuttingDownError:
        log_event(
            logger, logging.INFO, "request_rejected",
            chat=scoped_id(chat_id), reason="shutting_down",
        )
        await reply(ui("shutting_down", language))
        return

    if is_duplicate:
        log_event(
            logger, logging.INFO, "duplicate_request",
            chat=scoped_id(chat_id), job_id=job.id,
        )
        await reply(format_duplicate(job, language=language))
        return
    log_event(
        logger, logging.INFO, "request_accepted",
        chat=scoped_id(chat_id), job_id=job.id, language=language,
    )
    await reply(format_accepted(job.request, language=language))


async def _safe_reply(message: object, text: str) -> None:
    """Adapter-level guard: a failed Telegram send must not crash the bot."""
    try:
        lines = text.splitlines() or [""]
        for chunk in chunk_lines(lines, DEFAULT_MAX_MESSAGE_LENGTH):
            await message.reply_text(chunk)  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - Telegram/API send failures are logged, not raised
        chat_id = getattr(message, "chat_id", None)
        log_exception_event(
            logger, "reply_send_failure",
            chat=scoped_id(chat_id) if chat_id is not None else "unknown",
        )


async def _safe_reply_with_keyboard(
    message: object, text: str, buttons: list[tuple[str, str]],
) -> None:
    """Adapter-level guard: send an inline-keyboard prompt (Stage 31.1 language pick)."""
    try:
        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton(label, callback_data=data)] for label, data in buttons
        ])
        await message.reply_text(text, reply_markup=markup)  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - Telegram/API send failures are logged, not raised
        chat_id = getattr(message, "chat_id", None)
        log_exception_event(
            logger, "language_prompt_send_failure",
            chat=scoped_id(chat_id) if chat_id is not None else "unknown",
        )


async def _safe_reply_document(message: object, data: bytes, filename: str) -> None:
    """Adapter-level guard: a failed Telegram document send must not crash the bot."""
    try:
        await message.reply_document(document=data, filename=filename)  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - Telegram/API send failures are logged, not raised
        chat_id = getattr(message, "chat_id", None)
        log_exception_event(
            logger, "export_send_failure",
            chat=scoped_id(chat_id) if chat_id is not None else "unknown",
        )


def _export_filename(job: Job) -> str:
    raw = f"{job.request.brand}_{job.request.model}"
    safe = _EXPORT_FILENAME_SAFE_PATTERN.sub("_", raw).strip("_") or "product"
    return f"verification_{safe}.csv"


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

        async def prompt_language(text: str, buttons: list[tuple[str, str]]) -> None:
            await _safe_reply_with_keyboard(update.message, text, buttons)

        await handle_product_query(text, chat_id, manager, reply=reply, prompt_language=prompt_language)

    return verify_command


def build_language_callback(manager: JobManager):
    """Bind the RU/EN inline-keyboard CallbackQueryHandler to ``manager``."""

    async def language_callback(update, context) -> None:  # noqa: ANN001
        callback_query = update.callback_query
        data = str(getattr(callback_query, "data", "") or "")
        chat_id = callback_query.message.chat_id
        try:
            await callback_query.answer()
        except Exception:  # noqa: BLE001 - answering is a courtesy, never fatal
            log_exception_event(
                logger, "language_callback_answer_failure", chat=scoped_id(chat_id),
            )
        if not data.startswith(_LANGUAGE_CALLBACK_PREFIX):
            return
        language = normalize_language(data[len(_LANGUAGE_CALLBACK_PREFIX):])
        request = manager.pop_pending_query(chat_id)
        if request is None:
            # Stale/duplicate tap -- the pending query was already consumed
            # (or the process restarted and lost in-memory state).
            log_event(
                logger, logging.INFO, "language_choice_no_pending_query",
                chat=scoped_id(chat_id),
            )
            return

        async def reply(chunk: str) -> None:
            await _safe_reply(callback_query.message, chunk)

        await _start_verification(request, chat_id, manager, language=language, reply=reply)
        try:
            await callback_query.edit_message_reply_markup(reply_markup=None)
        except Exception:  # noqa: BLE001 - removing the keyboard is cosmetic, never fatal
            pass

    return language_callback


def build_status_command(manager: JobManager):
    async def status_command(update, context) -> None:  # noqa: ANN001
        chat_id = _chat_id(update)
        jobs = manager.active_jobs_for_chat(chat_id)
        log_event(
            logger, logging.INFO, "status_action",
            chat=scoped_id(chat_id), active_jobs=len(jobs),
        )
        await _safe_reply(update.message, format_status(jobs))

    return status_command


def build_cancel_command(manager: JobManager):
    async def cancel_command(update, context) -> None:  # noqa: ANN001
        chat_id = _chat_id(update)
        count = await manager.cancel_chat_jobs(chat_id)
        log_event(
            logger, logging.INFO, "cancel_action",
            chat=scoped_id(chat_id), affected_jobs=count,
        )
        await _safe_reply(update.message, format_cancelled(count))

    return cancel_command


def build_export_command(manager: JobManager):
    async def export_command(update, context) -> None:  # noqa: ANN001
        chat_id = _chat_id(update)
        job = manager.last_export_job_for_chat(chat_id)
        if job is None or job.result is None:
            log_event(logger, logging.INFO, "export_no_result", chat=scoped_id(chat_id))
            await _safe_reply(update.message, EXPORT_NO_RESULT_MESSAGE)
            return
        csv_text = export_result_csv(job.result, language=job.language)
        log_event(
            logger, logging.INFO, "export_sent",
            chat=scoped_id(chat_id), job_id=job.id,
        )
        await _safe_reply_document(
            update.message, csv_text.encode("utf-8-sig"), _export_filename(job),
        )

    return export_command
