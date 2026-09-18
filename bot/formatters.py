"""Deterministic VerifyProductResult -> Telegram message text formatting.

Only depends on services.product_verifier's stable DTOs (VerifyProductResult,
ServiceAttribute, ...) plus bot.jobs.Job (a bot-layer, non-core DTO) --
never on core.* or ProductWorkflowResult. Pure string-building: every
function here is unit-testable without a bot framework or network.

Stage 12's format_result()/format_error() are unchanged below; Stage 13
only adds job-lifecycle formatter helpers (accepted/started/duplicate/
status/cancelled/outcome) that reuse them for the final result.
"""

from __future__ import annotations

import re

from bot.jobs import Job
from services.product_verifier import (
    ServiceAttribute,
    ServiceEvidence,
    VerifyProductRequest,
    VerifyProductResult,
)


# Telegram's hard cap is 4096 UTF-16 code units per message.  The default
# keeps a generous margin, and chunk_lines also caps caller-provided limits at
# 4095 code units so every returned chunk is strictly below Telegram's cap.
TELEGRAM_MESSAGE_LIMIT = 4096
DEFAULT_MAX_MESSAGE_LENGTH = 3500
DEFAULT_MAX_ATTRIBUTES = 12
DEFAULT_MAX_REASONS = 2

QUALITY_LABELS = {
    "verified": "✅ Данные подтверждены",
    "partial": "\U0001f7e1 Данные подтверждены частично",
    "insufficient": "⚠️ Недостаточно данных для проверки",
    "conflicted": "❗ Источники расходятся по части характеристик",
}

CONFIDENCE_LABELS = {"high": "высокая", "medium": "средняя", "low": "низкая"}

ERROR_MESSAGES = {
    "invalid_request": "Не удалось распознать запрос: {message}",
    "workflow_failure": "Не удалось получить данные о товаре: {message}. Попробуйте ещё раз позже.",
    "internal_error": "Произошла непредвиденная ошибка. Попробуйте позже или сообщите администратору.",
}

PRIORITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3}

_STATUS_ICON = {"Confirmed": "✅", "Conflict": "❗"}

_CATEGORY_UNKNOWN_REASON = (
    "Category could not be determined, so category-specific critical fields "
    "cannot be evaluated."
)
_IDENTITY_EVIDENCE_PREFIX = "Core identity field(s) have no confirming evidence: "
_LOW_IDENTITY_REASON = "Product identity was resolved with low confidence."
_CANDIDATE_CODE_WARNING = (
    "Identity carries an unresolved candidate code that was not assigned "
    "model/SKU semantics."
)
_LOW_CATEGORY_WARNING = "Category confidence is low."

_COVERAGE_REASON_PATTERN = re.compile(
    r"Coverage \((?P<coverage>[^)]+)\) and/or critical-field discovery "
    r"\((?P<critical>[^)]+) of (?P<total>\d+)\) fall below the minimum useful threshold\."
)
_UNRESOLVED_WARNING_PATTERN = re.compile(
    r"(?P<count>\d+) expected attribute\(s\) remain unresolved\."
)
_SPECIALIZED_SOURCE_WARNING_PATTERN = re.compile(
    r"(?P<count>\d+) of (?P<total>\d+) confirmed critical field\(s\) rely on "
    r"specialized-reference evidence rather than a manufacturer-verified source\."
)
_NON_CRITICAL_CONFLICT_PATTERN = re.compile(
    r"(?P<count>\d+) non-critical attribute\(s\) have conflicting evidence: "
    r"(?P<fields>.+)\."
)
_INTERNAL_REASON_CODE_PATTERN = re.compile(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)+")
_EXCEPTION_NAME_PATTERN = re.compile(r"\b[A-Za-z]+(?:Error|Exception)\b")

_IDENTITY_FIELD_LABELS = {
    "brand": "бренд",
    "model": "модель",
}


def format_attribute_value(value: object, unit: str | None) -> str:
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
        message = ERROR_MESSAGES["internal_error"]
        return chunk_lines([message], DEFAULT_MAX_MESSAGE_LENGTH)
    template = ERROR_MESSAGES.get(error.kind, ERROR_MESSAGES["internal_error"])
    if error.kind == "internal_error":
        message = template
    else:
        message = template.format(message=error.message)
    return chunk_lines([message], DEFAULT_MAX_MESSAGE_LENGTH)


def _sorted_attributes(attributes: list[ServiceAttribute]) -> list[ServiceAttribute]:
    return sorted(
        attributes,
        key=lambda item: (PRIORITY_RANK.get(item.priority, 3), item.canonical_name),
    )


# Stage 30 provenance: source_type values are set by core.discovery.classify_source
# (manufacturer/official_document = official; marketplace/retailer/
# specialized_reference/other/unknown = secondary). Only this fixed set maps
# to "official" -- everything else, including an unrecognized or future
# source_type string, safely falls back to "secondary" rather than ever
# echoing the raw internal value to the user.
_OFFICIAL_SOURCE_TYPES = {"manufacturer", "official_document"}


def _provenance_suffix(evidence: ServiceEvidence) -> str:
    kind = "официальный источник" if evidence.source_type in _OFFICIAL_SOURCE_TYPES else "сторонний источник"
    source = (evidence.source or "").strip()
    return f"\U0001f517 {kind}: {source}" if source else f"\U0001f517 {kind}"


def _confirmed_provenance_line(attribute: ServiceAttribute) -> str | None:
    """One compact provenance line for a Confirmed attribute, if evidence exists.

    Only Confirmed attributes get provenance (per Stage 30 spec) -- an
    Unresolved/Conflict attribute has no single confirming source to show.
    """
    if not attribute.supporting_sources:
        return None
    return f"    {_provenance_suffix(attribute.supporting_sources[0])}"


def _attribute_line(attribute: ServiceAttribute) -> str:
    if attribute.status == "Confirmed":
        icon = _STATUS_ICON["Confirmed"]
        value_text = format_attribute_value(attribute.value, attribute.unit)
        line = f"{icon} {attribute.display_name}: {value_text}"
        provenance = _confirmed_provenance_line(attribute)
        return f"{line}\n{provenance}" if provenance else line
    if attribute.status == "Conflict":
        icon = _STATUS_ICON["Conflict"]
        return f"{icon} {attribute.display_name}: данные расходятся"
    value_text = format_attribute_value(attribute.value, attribute.unit)
    return f"\U0001f539 {attribute.display_name}: {value_text} (не подтверждено)"


def _fallback_display_name(canonical_name: str) -> str:
    return canonical_name.replace("_", " ").strip() or canonical_name


def _localize_quality_message(value: str) -> str:
    """Translate known structured quality messages for Telegram presentation.

    The service/core contract remains untouched. Unknown human-readable text
    is returned unchanged so a new upstream reason cannot break formatting or
    silently lose its meaning; traceback-like text and bare internal codes are
    replaced with a safe user-facing fallback.
    """
    normalized = " ".join(value.split())
    if normalized == _CATEGORY_UNKNOWN_REASON:
        return (
            "Категорию товара определить не удалось, поэтому критически важные "
            "характеристики для неё нельзя оценить."
        )
    if normalized.startswith(_IDENTITY_EVIDENCE_PREFIX) and normalized.endswith("."):
        raw_fields = normalized[len(_IDENTITY_EVIDENCE_PREFIX):-1]
        fields = ", ".join(
            _IDENTITY_FIELD_LABELS.get(field.strip(), _fallback_display_name(field.strip()))
            for field in raw_fields.split(",")
            if field.strip()
        )
        if fields:
            return f"Нет подтверждающих данных для основных полей товара: {fields}."
    if normalized == _LOW_IDENTITY_REASON:
        return "Товар определён с низкой уверенностью."
    if match := _COVERAGE_REASON_PATTERN.fullmatch(normalized):
        return (
            f"Покрытие данных ({match['coverage']}) и/или найденные критически важные "
            f"характеристики ({match['critical']} из {match['total']}) ниже минимально "
            "полезного уровня."
        )
    if match := _UNRESOLVED_WARNING_PATTERN.fullmatch(normalized):
        return f"Остались неопределённые характеристики: {match['count']}."
    if match := _SPECIALIZED_SOURCE_WARNING_PATTERN.fullmatch(normalized):
        return (
            f"Для {match['count']} из {match['total']} подтверждённых критически важных "
            "характеристик использованы специализированные источники вместо источника "
            "производителя."
        )
    if match := _NON_CRITICAL_CONFLICT_PATTERN.fullmatch(normalized):
        fields = ", ".join(
            _fallback_display_name(field.strip())
            for field in match["fields"].split(",")
            if field.strip()
        )
        suffix = f": {fields}" if fields else ""
        return (
            f"Для некритичных характеристик обнаружены противоречивые данные "
            f"({match['count']}){suffix}."
        )
    if normalized == _CANDIDATE_CODE_WARNING:
        return (
            "Обнаружен возможный код товара, но недостаточно данных, чтобы считать "
            "его моделью или артикулом."
        )
    if normalized == _LOW_CATEGORY_WARNING:
        return "Категория товара определена с низкой уверенностью."
    if (
        "traceback" in normalized.casefold()
        or _INTERNAL_REASON_CODE_PATTERN.fullmatch(normalized)
        or _EXCEPTION_NAME_PATTERN.search(normalized)
    ):
        return "Дополнительных подтверждённых данных недостаточно."
    return normalized


def _status_section_lines(
    result: VerifyProductResult,
    *,
    max_attributes: int,
) -> list[str]:
    """Render bounded lists from the stable result's structured status fields."""
    limit = max(0, max_attributes)
    by_name = {item.canonical_name: item for item in result.attributes}
    confirmed = _sorted_attributes([
        item for item in result.attributes if item.status == "Confirmed"
    ])

    conflict_names = list(dict.fromkeys(result.conflicts))
    for item in result.attributes:
        if item.status == "Conflict" and item.canonical_name not in conflict_names:
            conflict_names.append(item.canonical_name)

    unresolved_names = list(dict.fromkeys(result.unresolved))
    for item in result.attributes:
        if (
            item.status not in ("Confirmed", "Conflict")
            and item.canonical_name not in unresolved_names
        ):
            unresolved_names.append(item.canonical_name)

    lines: list[str] = []

    def add_section(title: str, rendered: list[str]) -> None:
        if not rendered:
            return
        shown = rendered[:limit]
        lines.extend(("", f"{title} ({len(rendered)}):", *shown))
        remaining = len(rendered) - len(shown)
        if remaining > 0:
            lines.append(f"… и ещё {remaining}")

    add_section(
        "✅ Подтверждено",
        [_attribute_line(item) for item in confirmed],
    )

    conflict_lines = []
    for name in conflict_names:
        attribute = by_name.get(name)
        display_name = attribute.display_name if attribute else _fallback_display_name(name)
        conflict_lines.append(f"❗ {display_name}: данные расходятся")
    add_section("❗ Конфликты", conflict_lines)

    unresolved_lines = []
    for name in unresolved_names:
        attribute = by_name.get(name)
        display_name = attribute.display_name if attribute else _fallback_display_name(name)
        if attribute is not None and attribute.value is not None:
            value_text = format_attribute_value(attribute.value, attribute.unit)
            unresolved_lines.append(
                f"🔹 {display_name}: {value_text} (не подтверждено)"
            )
        else:
            unresolved_lines.append(f"▫️ {display_name}")
    add_section("▫️ Не определено", unresolved_lines)
    return lines


def _insufficient_reason_lines(result: VerifyProductResult) -> list[str]:
    quality = result.quality
    if quality is None or quality.status != "insufficient":
        return []
    reasons: list[str] = []
    for value in (*quality.reasons, *quality.warnings):
        localized = _localize_quality_message(value)
        if localized and localized not in reasons:
            reasons.append(localized)
        if len(reasons) == DEFAULT_MAX_REASONS:
            break
    if not reasons:
        return []
    return ["", "Почему данных недостаточно:", *(f"• {item}" for item in reasons)]


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


def utf16_code_units(text: str) -> int:
    """Return Telegram's message-length unit without encoding side effects."""
    return sum(2 if ord(character) > 0xFFFF else 1 for character in text)


def _truncate_utf16(text: str, max_units: int) -> str:
    if utf16_code_units(text) <= max_units:
        return text
    ellipsis = "…"
    budget = max_units - utf16_code_units(ellipsis)
    if budget <= 0:
        return ellipsis[:max_units]
    used = 0
    kept: list[str] = []
    for character in text:
        character_units = 2 if ord(character) > 0xFFFF else 1
        if used + character_units > budget:
            break
        kept.append(character)
        used += character_units
    return "".join(kept) + ellipsis


def chunk_lines(lines: list[str], max_length: int) -> list[str]:
    """Group lines by UTF-16 code units, joined by ``\\n``.

    Never splits a single line across chunks; a single line longer than
    the effective limit is truncated with an ellipsis.  The effective limit
    is always below Telegram's 4096 UTF-16-code-unit hard cap.
    """
    if max_length < 1:
        raise ValueError("max_length must be at least 1")
    effective_limit = min(max_length, TELEGRAM_MESSAGE_LIMIT - 1)
    chunks: list[str] = []
    current: list[str] = []
    current_length = 0
    for line in lines:
        line = _truncate_utf16(line, effective_limit)
        line_length = utf16_code_units(line)
        added_length = line_length + (1 if current else 0)
        if current and current_length + added_length > effective_limit:
            chunks.append("\n".join(current))
            current = [line]
            current_length = line_length
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
    lines.extend(_insufficient_reason_lines(result))
    lines.extend(_status_section_lines(result, max_attributes=max_attributes))

    return chunk_lines(lines, max_message_length)


# ---------------------------------------------------------------------------
# Stage 13: job-lifecycle formatting.
# ---------------------------------------------------------------------------

JOB_STATE_LABELS = {
    "queued": "в очереди",
    "running": "выполняется",
    "completed": "завершено",
    "failed": "не удалось",
    "cancelled": "отменено",
}


def _product_name(request: VerifyProductRequest) -> str:
    return f"{request.brand} {request.model}".strip()


def format_accepted(request: VerifyProductRequest) -> str:
    """Sent immediately after a new job is created -- the required "принял" ack."""
    return f"Принял запрос. Проверяю {_product_name(request)}…"


def format_started(request: VerifyProductRequest) -> str:
    """Sent once, when a job actually begins running (leaves "queued")."""
    return f"\U0001f504 Начинаю проверку {_product_name(request)}…"


def format_duplicate(job: Job) -> str:
    """Sent instead of creating a second job for an already-active request."""
    state_label = JOB_STATE_LABELS.get(job.state, job.state)
    return (
        f"Запрос на {_product_name(job.request)} уже выполняется ({state_label}). "
        "Дождитесь результата или используйте /status."
    )


def format_status(jobs: list[Job]) -> str:
    """Rendered for /status: a chat's currently active (queued/running) jobs."""
    if not jobs:
        return "Активных задач нет."
    lines = ["Ваши активные задачи:"]
    for job in jobs:
        state_label = JOB_STATE_LABELS.get(job.state, job.state)
        lines.append(f"• {_product_name(job.request)} — {state_label}")
    return "\n".join(lines)


def format_cancelled(count: int) -> str:
    """Sent as the immediate reply to /cancel."""
    if count == 0:
        return "Нет активных задач для отмены."
    if count == 1:
        return "Отменена 1 задача."
    return f"Отменено задач: {count}."


def format_job_outcome(job: Job) -> list[str]:
    """The final message(s) for a job that reached a terminal state.

    A cancelled job intentionally produces no message: the user already got
    an immediate /cancel confirmation, and cooperative cancellation means a
    running computation's result (if it finishes anyway) must be discarded,
    never delivered.
    """
    if job.state == "completed" and job.result is not None:
        return format_result(job.result)
    if job.state == "failed":
        if job.result is not None:
            return format_result(job.result)
        return [ERROR_MESSAGES["internal_error"]]
    return []
