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

import asyncio
import logging
import re
from urllib.parse import urlparse
from typing import Awaitable, Callable

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, LinkPreviewOptions

from bot.export import export_result_csv
from bot.discovery_formatters import format_discovery_result
from bot.formatters import (
    DEFAULT_MAX_MESSAGE_LENGTH,
    chunk_lines,
    format_accepted,
    format_cancelled,
    format_auxiliary,
    format_duplicate,
    format_job_outcome,
    product_image_records,
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
from services.discovery_debug import DiscoveryDebugResult, DiscoveryDebugService


logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

Reply = Callable[[str], Awaitable[object]]
PromptLanguage = Callable[[str, list[tuple[str, str]]], Awaitable[object]]
ReplyCard = Callable[[str, str | None], Awaitable[object]]

START_MESSAGE = ui("start_message", DEFAULT_LANGUAGE)
HELP_MESSAGE = ui("help_message", DEFAULT_LANGUAGE)
PARSE_ERROR_MESSAGE = ui("parse_error", DEFAULT_LANGUAGE)
SHUTTING_DOWN_MESSAGE = ui("shutting_down", DEFAULT_LANGUAGE)
EXPORT_NO_RESULT_MESSAGE = ui("export_no_result", DEFAULT_LANGUAGE)

_LANGUAGE_CALLBACK_PREFIX = "lang:"
_PHOTOS_CALLBACK_PREFIX = "photos:"
_MAX_MEDIA_GROUP = 10  # Telegram's per-album limit

_EXPORT_FILENAME_SAFE_PATTERN = re.compile(r"[^0-9A-Za-zА-Яа-яЁё]+")


async def handle_discovery_query(
    product_name: str,
    chat_id: int,
    service: DiscoveryDebugService,
    *,
    reply: Reply,
    include_all_rejected: bool = False,
) -> DiscoveryDebugResult | None:
    """Run the Stage 33.0 source-only flow; never starts verification."""
    normalized = " ".join((product_name or "").split())
    if not normalized:
        previous = service.last_result(chat_id) if include_all_rejected else None
        if previous is None:
            await reply(
                "Укажите название товара: /discover Google Pixel 9 Pro"
            )
            return None
        result = previous
    else:
        await reply(f"🔎 Ищу страницы модели: {normalized}")
        try:
            result = await asyncio.to_thread(
                service.discover_name, normalized, chat_id=chat_id,
            )
        except Exception as error:  # noqa: BLE001 - debug flow reports a bounded failure
            log_exception_event(
                logger, "discovery_debug_failure", chat=scoped_id(chat_id),
                error_type=type(error).__name__,
            )
            await reply(f"Discovery завершился ошибкой: {type(error).__name__}: {error}")
            return None
    for chunk in format_discovery_result(result, include_all_rejected=include_all_rejected):
        await reply(chunk)
    return result


def build_discovery_command(
    service: DiscoveryDebugService, *, include_all_rejected: bool = False,
):
    async def discovery_command(update, context) -> None:  # noqa: ANN001
        text = str(getattr(update.message, "text", "") or "")
        product_name = text.split(maxsplit=1)[1] if len(text.split(maxsplit=1)) == 2 else ""

        async def reply(chunk: str) -> None:
            await _safe_reply(update.message, chunk)

        await handle_discovery_query(
            product_name, _chat_id(update), service, reply=reply,
            include_all_rejected=include_all_rejected,
        )

    return discovery_command


def build_discovery_text_handler(service: DiscoveryDebugService):
    """Plain name-only messages in the opt-in Stage 33.0 bot mode."""

    async def discovery_text(update, context) -> None:  # noqa: ANN001
        async def reply(chunk: str) -> None:
            await _safe_reply(update.message, chunk)

        await handle_discovery_query(
            str(getattr(update.message, "text", "") or ""),
            _chat_id(update), service, reply=reply,
        )

    return discovery_text


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
    prompt_photos: PromptLanguage | None = None,
    reply_card: ReplyCard | None = None,
) -> None:
    """Submit the job and reply immediately (accepted/duplicate); Stage 13 flow."""

    async def on_update(job: Job) -> None:
        if job.state == "running":
            await reply(format_started(job.request, language=language))
            return
        for chunk in format_job_outcome(job, language=language):
            await reply(chunk)
        if job.state != "completed" or job.result is None:
            return
        # Stage 31.5: the official source owns spec priority, but the
        # secondary reference page keeps its rich preview and its
        # review/opinions/compare/pictures/prices links, below the result.
        card = format_auxiliary(job.result, language=language)
        if card is not None:
            text, preview_url = card
            if reply_card is not None:
                await reply_card(text, preview_url)
            else:
                await reply(text)
        # Offer the photo download only when the pipeline found safe product
        # photos (official first, secondary as a labeled top-up).
        if prompt_photos is not None:
            records = product_image_records(job.result)
            if records:
                official = sum(1 for _, is_official, _ in records if is_official)
                await prompt_photos(
                    ui(
                        "photos_prompt", language, count=len(records),
                        official=official, secondary=len(records) - official,
                    ),
                    [(ui("photos_button", language), f"{_PHOTOS_CALLBACK_PREFIX}{job.id}")],
                )

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


async def _safe_reply(
    message: object, text: str, *, preview_url: str | None = None,
) -> None:
    """Adapter-level guard: a failed Telegram send must not crash the bot.

    Link previews are off by default (the result message lists several
    sources); ``preview_url`` turns them on for exactly that URL, which is how
    the secondary-source card gets its rich preview without the official link
    (first in the result) stealing it.
    """
    try:
        if preview_url is not None:
            options = LinkPreviewOptions(is_disabled=False, url=preview_url)
        else:
            options = LinkPreviewOptions(is_disabled=True)
        lines = text.splitlines() or [""]
        for chunk in chunk_lines(lines, DEFAULT_MAX_MESSAGE_LENGTH):
            await message.reply_text(chunk, link_preview_options=options)  # type: ignore[attr-defined]
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

        async def reply_card(text: str, preview_url: str | None) -> None:
            await _safe_reply(callback_query.message, text, preview_url=preview_url)

        async def prompt_photos(text: str, buttons: list[tuple[str, str]]) -> None:
            await _safe_reply_with_keyboard(callback_query.message, text, buttons)

        await _start_verification(
            request, chat_id, manager, language=language, reply=reply,
            prompt_photos=prompt_photos, reply_card=reply_card,
        )
        try:
            await callback_query.edit_message_reply_markup(reply_markup=None)
        except Exception:  # noqa: BLE001 - removing the keyboard is cosmetic, never fatal
            pass

    return language_callback


async def _safe_send_photos(
    message: object, records: list[tuple[str, bool, str]], language: Language,
) -> None:
    """Send photos as albums, official group first; each group's first photo
    is captioned with its provenance. Falls back to plain links on failure."""
    urls = [url for url, _, _ in records]
    try:
        for is_official in (True, False):
            group = [item for item in records if item[1] == is_official]
            for start in range(0, len(group), _MAX_MEDIA_GROUP):
                batch = group[start:start + _MAX_MEDIA_GROUP]
                host = (urlparse(batch[0][2]).hostname or "").removeprefix("www.")
                caption = ui(
                    "photos_caption", language,
                    kind=ui("official_source" if is_official else "secondary_source", language),
                    host=host,
                ).rstrip(" \u00b7") if start == 0 else None
                if len(batch) == 1:
                    await message.reply_photo(photo=batch[0][0], caption=caption)  # type: ignore[attr-defined]
                else:
                    await message.reply_media_group(  # type: ignore[attr-defined]
                        media=[
                            InputMediaPhoto(media=url, caption=caption if index == 0 else None)
                            for index, (url, _, _) in enumerate(batch)
                        ],
                    )
    except Exception:  # noqa: BLE001 - Telegram/API send failures are logged, not raised
        chat_id = getattr(message, "chat_id", None)
        log_exception_event(
            logger, "photos_send_failure",
            chat=scoped_id(chat_id) if chat_id is not None else "unknown",
        )
        await _safe_reply(message, "\n".join([ui("photos_failed", language), *urls]))


def build_photos_callback(manager: JobManager):
    """Bind the "download all photos" button to ``manager``'s finished jobs."""

    async def photos_callback(update, context) -> None:  # noqa: ANN001
        callback_query = update.callback_query
        data = str(getattr(callback_query, "data", "") or "")
        chat_id = callback_query.message.chat_id
        try:
            await callback_query.answer()
        except Exception:  # noqa: BLE001 - answering is a courtesy, never fatal
            log_exception_event(
                logger, "photos_callback_answer_failure", chat=scoped_id(chat_id),
            )
        if not data.startswith(_PHOTOS_CALLBACK_PREFIX):
            return
        job = manager.get_job(data[len(_PHOTOS_CALLBACK_PREFIX):])
        # A job id from another chat is never served, even if guessed.
        if job is None or job.chat_id != chat_id or job.result is None:
            await _safe_reply(callback_query.message, ui("photos_expired", DEFAULT_LANGUAGE))
            return
        records = product_image_records(job.result)
        if not records:
            await _safe_reply(callback_query.message, ui("photos_expired", job.language))
            return
        log_event(
            logger, logging.INFO, "photos_sent",
            chat=scoped_id(chat_id), job_id=job.id, count=len(records),
        )
        await _safe_send_photos(callback_query.message, records, job.language)

    return photos_callback


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
