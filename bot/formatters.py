"""Deterministic VerifyProductResult -> Telegram message text formatting.

Only depends on services.product_verifier's stable DTOs (VerifyProductResult,
ServiceAttribute, ...) -- never on core.* or ProductWorkflowResult. Pure
string-building: every function here is unit-testable without a bot
framework or network.
"""

from __future__ import annotations

from services.product_verifier import ServiceAttribute, VerifyProductResult


# Telegram's hard cap is 4096 UTF-16 code units per message. We stay well
# under it (and use plain character length, a superset bound for BMP text)
# so a slightly different counting rule can never push us over.
TELEGRAM_MESSAGE_LIMIT = 4096
DEFAULT_MAX_MESSAGE_LENGTH = 3500
DEFAULT_MAX_ATTRIBUTES = 12

QUALITY_LABELS = {
    "verified": "✅ Подтверждено (verified)",
    "partial": "\U0001f7e1 Частично подтверждено (partial)",
    "insufficient": "⚠️ Недостаточно данных (insufficient)",
    "conflicted": "❗ Обнаружены противоречия (conflicted)",
}

CONFIDENCE_LABELS = {"high": "высокая", "medium": "средняя", "low": "низкая"}

ERROR_MESSAGES = {
    "invalid_request": "Не удалось распознать запрос: {message}",
    "workflow_failure": "Не удалось получить данные о товаре: {message}. Попробуйте ещё раз позже.",
    "internal_error": "Произошла непредвиденная ошибка. Попробуйте позже или сообщите администратору.",
}

PRIORITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3}

_STATUS_ICON = {"Confirmed": "✅", "Conflict": "❗"}


def _format_value(value: object, unit: str | None) -> str:
    if value is None:
        return "—"
    if isinstance(value, dict) and {"height", "width", "depth", "unit"} <= value.keys():
        return f"{value['height']} × {value['width']} × {value['depth']} {value['unit']}".strip()
    text = str(value)
    if unit and not text.casefold().rstrip().endswith(str(unit).casefold()):
        return f"{text} {unit}"
    return text


def _format_age(seconds: float) -> str:
    if seconds < 60:
        return "меньше минуты назад"
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes} мин. назад"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} ч. назад"
    days = hours // 24
    return f"{days} дн. назад"


def format_error(result: VerifyProductResult) -> list[str]:
    """One short, user-safe message for a failed VerifyProductResult.

    Never includes a reason "kind" code, an exception type, or a traceback --
    only the human-facing template text plus the (already plain-string)
    error message.
    """
    error = result.error
    if error is None:
        return [ERROR_MESSAGES["internal_error"]]
    template = ERROR_MESSAGES.get(error.kind, ERROR_MESSAGES["internal_error"])
    if error.kind == "internal_error":
        return [template]
    return [template.format(message=error.message)]


def _eligible_attributes(attributes: tuple[ServiceAttribute, ...]) -> list[ServiceAttribute]:
    found = [
        item for item in attributes
        if item.status in ("Confirmed", "Conflict") or item.value is not None
    ]
    return sorted(
        found,
        key=lambda item: (PRIORITY_RANK.get(item.priority, 3), item.canonical_name),
    )


def _attribute_line(attribute: ServiceAttribute) -> str:
    if attribute.status == "Confirmed":
        icon = _STATUS_ICON["Confirmed"]
        value_text = _format_value(attribute.value, attribute.unit)
        return f"{icon} {attribute.display_name}: {value_text}"
    if attribute.status == "Conflict":
        icon = _STATUS_ICON["Conflict"]
        return f"{icon} {attribute.display_name}: конфликт данных"
    value_text = _format_value(attribute.value, attribute.unit)
    return f"\U0001f539 {attribute.display_name}: {value_text} (не подтверждено)"


def _summary_lines(result: VerifyProductResult) -> list[str]:
    identity = result.identity
    category = result.category
    quality = result.quality
    name = " ".join(filter(None, (
        identity.brand if identity else result.request.brand,
        (identity.commercial_model or identity.base_model) if identity else result.request.model,
    )))
    lines = [f"\U0001f4e6 {name}".rstrip()]
    if category is not None:
        confidence = CONFIDENCE_LABELS.get(category.confidence, category.confidence)
        lines.append(f"Категория: {category.category_name} ({confidence})")
    if quality is not None:
        status_label = QUALITY_LABELS.get(quality.status, quality.status)
        lines.append(f"Качество: {status_label}")
        lines.append(f"Покрытие схемы: {quality.coverage_percent}%")
        lines.append(
            f"Подтверждено: {quality.confirmed_count}/{quality.schema_total} "
            f"• Не определено: {quality.unresolved_count} "
            f"• Конфликты: {quality.conflict_count}"
        )
    if result.served_from_cache:
        age = _format_age(result.cache_age_seconds or 0.0)
        lines.append(f"♻️ Результат из кэша ({age})")
    return lines


def chunk_lines(lines: list[str], max_length: int) -> list[str]:
    """Group lines into blocks of <= max_length characters, joined by "\\n".

    Never splits a single line across chunks; a single line longer than
    max_length is truncated with an ellipsis so no chunk can ever exceed the
    limit, regardless of how verbose one line is.
    """
    chunks: list[str] = []
    current: list[str] = []
    current_length = 0
    for line in lines:
        if len(line) > max_length:
            line = line[: max_length - 1] + "…"
        added_length = len(line) + (1 if current else 0)
        if current and current_length + added_length > max_length:
            chunks.append("\n".join(current))
            current = [line]
            current_length = len(line)
        else:
            current.append(line)
            current_length += added_length
    if current:
        chunks.append("\n".join(current))
    return chunks


def format_result(
    result: VerifyProductResult,
    *,
    max_attributes: int = DEFAULT_MAX_ATTRIBUTES,
    max_message_length: int = DEFAULT_MAX_MESSAGE_LENGTH,
) -> list[str]:
    """Render a VerifyProductResult as one or more Telegram-safe messages."""
    if not result.success:
        return format_error(result)

    lines = _summary_lines(result)
    eligible = _eligible_attributes(result.attributes)
    shown = eligible[:max_attributes]
    if shown:
        lines.append("")
        lines.append("Основные характеристики:")
        lines.extend(_attribute_line(item) for item in shown)
    remaining = len(eligible) - len(shown)
    if remaining > 0:
        lines.append(f"… и ещё {remaining} характеристик(-и)")

    return chunk_lines(lines, max_message_length)
