"""Telegram-facing handlers.

Thin by design: parsing (bot.parser) and formatting (bot.formatters) are
pure functions tested without any bot framework, and the actual
verification logic lives entirely behind ``ProductVerifierService.verify()``
(services.product_verifier) -- this module never imports core.workflow,
core.profile, or core.quality. ``handle_product_query`` takes a plain
async ``reply`` callback instead of a python-telegram-bot ``Update``, so it
is directly unit-testable; the ``*_command`` functions are the only pieces
that touch python-telegram-bot's ``Update``/``Context`` types.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

from bot.formatters import ERROR_MESSAGES, format_result
from bot.parser import parse_product_query
from services.product_verifier import ProductVerifierService, VerifyProductRequest


logger = logging.getLogger(__name__)

Reply = Callable[[str], Awaitable[object]]

START_MESSAGE = (
    "Привет! Я проверяю характеристики товара по бренду и модели.\n\n"
    "Отправьте сообщение в формате:\n"
    "Bosch PUE611BB5E\n"
    "или\n"
    "Bosch | PUE611BB5E\n\n"
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
    "Dreame | G12 Pro | HHR32A"
)

PARSE_ERROR_MESSAGE = (
    "Не удалось понять запрос. Отправьте бренд и модель, например:\n"
    "Bosch PUE611BB5E\n"
    "или Bosch | PUE611BB5E"
)


async def handle_product_query(
    text: str,
    service: ProductVerifierService,
    *,
    reply: Reply,
) -> None:
    """Parse, verify, and reply -- the whole flow, independent of Telegram.

    ``service.verify()`` is blocking (it may run the live pipeline), so it
    runs in a worker thread via ``asyncio.to_thread`` and never blocks the
    bot's event loop.
    """
    query = parse_product_query(text)
    if query is None:
        await reply(PARSE_ERROR_MESSAGE)
        return

    request = VerifyProductRequest(brand=query.brand, model=query.model, article=query.article)
    try:
        result = await asyncio.to_thread(service.verify, request)
    except Exception:  # noqa: BLE001 - a fake/broken service must not crash the bot
        logger.exception("Unexpected failure while calling ProductVerifierService.verify")
        await reply(ERROR_MESSAGES["internal_error"])
        return

    for chunk in format_result(result):
        await reply(chunk)


async def _safe_reply(message: object, text: str) -> None:
    """Adapter-level guard: a failed Telegram send must not crash the bot."""
    try:
        await message.reply_text(text)  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - Telegram/API send failures are logged, not raised
        logger.exception("Failed to send a Telegram reply")


async def start_command(update, context) -> None:  # noqa: ANN001 - telegram.ext handler signature
    await _safe_reply(update.message, START_MESSAGE)


async def help_command(update, context) -> None:  # noqa: ANN001 - telegram.ext handler signature
    await _safe_reply(update.message, HELP_MESSAGE)


def build_verify_command(service: ProductVerifierService):
    """Bind a verify handler to ``service`` via closure -- no global state."""

    async def verify_command(update, context) -> None:  # noqa: ANN001
        text = update.message.text or ""

        async def reply(chunk: str) -> None:
            await _safe_reply(update.message, chunk)

        await handle_product_query(text, service, reply=reply)

    return verify_command
