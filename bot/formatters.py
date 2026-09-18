"""Deterministic VerifyProductResult -> Telegram message text formatting.

Only depends on services.product_verifier's stable DTOs (VerifyProductResult,
ServiceAttribute, ...) plus bot.jobs.Job (a bot-layer, non-core DTO) and
bot.i18n/bot.attribute_filter (also bot-layer) -- never on core.* or
ProductWorkflowResult. Pure string-building: every function here is
unit-testable without a bot framework or network.

Stage 12's format_result()/format_error() are unchanged in spirit below;
Stage 13 added job-lifecycle formatter helpers (accepted/started/duplicate/
status/cancelled/outcome) that reuse them for the final result. Stage 31.1
adds a ``language`` parameter throughout (RU/EN; see bot.i18n) and filters
out extraction noise / semantic duplicates before rendering (see
bot.attribute_filter) -- the underlying VerifyProductResult is untouched.
"""

from __future__ import annotations

import re

from bot.attribute_filter import filter_user_facing
from bot.i18n import (
    DEFAULT_LANGUAGE,
    Language,
    confidence_label,
    display_name,
    error_template,
    identity_field_label,
    job_state_label,
    quality_label,
    ui,
)
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


def format_attribute_value(value: object, unit: str | None) -> str:
    if value is None:
        return "—"
    if isinstance(value, dict) and {"height", "width", "depth", "unit"} <= value.keys():
        return f"{value['height']} × {value['width']} × {value['depth']} {value['unit']}".strip()
    text = str(value)
    if unit and not text.casefold().rstrip().endswith(str(unit).casefold()):
        return f"{text} {unit}"
    return text


def _format_age(seconds: float, language: Language) -> str:
    if seconds < 60:
        return ui("age_seconds", language)
    minutes = int(seconds // 60)
    if minutes < 60:
        return ui("age_minutes", language, n=minutes)
    hours = minutes // 60
    if hours < 24:
        return ui("age_hours", language, n=hours)
    days = hours // 24
    return ui("age_days", language, n=days)


def format_error(
    result: VerifyProductResult, *, language: Language = DEFAULT_LANGUAGE,
) -> list[str]:
    """One short, user-safe message for a failed VerifyProductResult.

    Never includes a reason "kind" code, an exception type, or a traceback --
    only the human-facing template text plus the (already plain-string)
    error message.
    """
    error = result.error
    if error is None:
        message = error_template("internal_error", language)
        return chunk_lines([message], DEFAULT_MAX_MESSAGE_LENGTH)
    template = error_template(error.kind, language)
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


def _provenance_suffix(evidence: ServiceEvidence, language: Language) -> str:
    kind = ui("official_source", language) if evidence.source_type in _OFFICIAL_SOURCE_TYPES else ui("secondary_source", language)
    source = (evidence.source or "").strip()
    return f"\U0001f517 {kind}: {source}" if source else f"\U0001f517 {kind}"


def _confirmed_provenance_line(attribute: ServiceAttribute, language: Language) -> str | None:
    """One compact provenance line for a Confirmed attribute, if evidence exists.

    Only Confirmed attributes get provenance (per Stage 30 spec) -- an
    Unresolved/Conflict attribute has no single confirming source to show.
    """
    if not attribute.supporting_sources:
        return None
    return f"    {_provenance_suffix(attribute.supporting_sources[0], language)}"


def _localized_name(attribute: ServiceAttribute, language: Language) -> str:
    return display_name(attribute.canonical_name, language, fallback=attribute.display_name)


def _attribute_line(attribute: ServiceAttribute, language: Language) -> str:
    name = _localized_name(attribute, language)
    if attribute.status == "Confirmed":
        icon = _STATUS_ICON["Confirmed"]
        value_text = format_attribute_value(attribute.value, attribute.unit)
        line = f"{icon} {name}: {value_text}"
        provenance = _confirmed_provenance_line(attribute, language)
        return f"{line}\n{provenance}" if provenance else line
    if attribute.status == "Conflict":
        icon = _STATUS_ICON["Conflict"]
        return f"{icon} {name}: {ui('conflict_suffix', language)}"
    value_text = format_attribute_value(attribute.value, attribute.unit)
    return f"\U0001f539 {name}: {value_text} ({ui('unconfirmed_suffix', language)})"


def _fallback_display_name(canonical_name: str) -> str:
    return canonical_name.replace("_", " ").strip() or canonical_name


def _localize_quality_message(value: str, language: Language) -> str:
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
            if language == "ru" else
            "The product category could not be determined, so its critical "
            "attributes cannot be evaluated."
        )
    if normalized.startswith(_IDENTITY_EVIDENCE_PREFIX) and normalized.endswith("."):
        raw_fields = normalized[len(_IDENTITY_EVIDENCE_PREFIX):-1]
        fields = ", ".join(
            identity_field_label(field.strip(), language) or _fallback_display_name(field.strip())
            for field in raw_fields.split(",")
            if field.strip()
        )
        if fields:
            return (
                f"Нет подтверждающих данных для основных полей товара: {fields}."
                if language == "ru" else
                f"No confirming data for core product field(s): {fields}."
            )
    if normalized == _LOW_IDENTITY_REASON:
        return (
            "Товар определён с низкой уверенностью." if language == "ru"
            else "The product was identified with low confidence."
        )
    if match := _COVERAGE_REASON_PATTERN.fullmatch(normalized):
        return (
            f"Покрытие данных ({match['coverage']}) и/или найденные критически важные "
            f"характеристики ({match['critical']} из {match['total']}) ниже минимально "
            "полезного уровня."
            if language == "ru" else
            f"Coverage ({match['coverage']}) and/or discovered critical attributes "
            f"({match['critical']} of {match['total']}) are below the minimum useful threshold."
        )
    if match := _UNRESOLVED_WARNING_PATTERN.fullmatch(normalized):
        return (
            f"Остались неопределённые характеристики: {match['count']}."
            if language == "ru" else
            f"Unresolved expected attributes remaining: {match['count']}."
        )
    if match := _SPECIALIZED_SOURCE_WARNING_PATTERN.fullmatch(normalized):
        return (
            f"Для {match['count']} из {match['total']} подтверждённых критически важных "
            "характеристик использованы специализированные источники вместо источника "
            "производителя."
            if language == "ru" else
            f"{match['count']} of {match['total']} confirmed critical attribute(s) rely on "
            "a specialized-reference source rather than a manufacturer-verified one."
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
            if language == "ru" else
            f"Non-critical attribute(s) have conflicting evidence ({match['count']}){suffix}."
        )
    if normalized == _CANDIDATE_CODE_WARNING:
        return (
            "Обнаружен возможный код товара, но недостаточно данных, чтобы считать "
            "его моделью или артикулом."
            if language == "ru" else
            "A possible product code was found, but there isn't enough data to treat "
            "it as a model or article number."
        )
    if normalized == _LOW_CATEGORY_WARNING:
        return (
            "Категория товара определена с низкой уверенностью." if language == "ru"
            else "The product category was determined with low confidence."
        )
    if (
        "traceback" in normalized.casefold()
        or _INTERNAL_REASON_CODE_PATTERN.fullmatch(normalized)
        or _EXCEPTION_NAME_PATTERN.search(normalized)
    ):
        return (
            "Дополнительных подтверждённых данных недостаточно." if language == "ru"
            else "Not enough additional confirmed data."
        )
    return normalized


def _status_section_lines(
    result: VerifyProductResult,
    *,
    max_attributes: int,
    language: Language,
) -> list[str]:
    """Render bounded lists from the stable result's structured status fields."""
    limit = max(0, max_attributes)
    attributes = filter_user_facing(result.attributes)
    shown_names = {item.canonical_name for item in attributes}
    by_name = {item.canonical_name: item for item in attributes}
    confirmed = _sorted_attributes([
        item for item in attributes if item.status == "Confirmed"
    ])

    conflict_names = [
        name for name in dict.fromkeys(result.conflicts) if name in shown_names
    ]
    for item in attributes:
        if item.status == "Conflict" and item.canonical_name not in conflict_names:
            conflict_names.append(item.canonical_name)

    unresolved_names = [
        name for name in dict.fromkeys(result.unresolved) if name in shown_names
    ]
    for item in attributes:
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
            lines.append(ui("more_items", language, count=remaining))

    add_section(
        ui("section_confirmed", language),
        [_attribute_line(item, language) for item in confirmed],
    )

    conflict_lines = []
    for name in conflict_names:
        attribute = by_name.get(name)
        name_text = display_name(name, language) if attribute is None else _localized_name(attribute, language)
        conflict_lines.append(f"❗ {name_text}: {ui('conflict_suffix', language)}")
    add_section(ui("section_conflicts", language), conflict_lines)

    unresolved_lines = []
    for name in unresolved_names:
        attribute = by_name.get(name)
        name_text = display_name(name, language) if attribute is None else _localized_name(attribute, language)
        if attribute is not None and attribute.value is not None:
            value_text = format_attribute_value(attribute.value, attribute.unit)
            unresolved_lines.append(
                f"🔹 {name_text}: {value_text} ({ui('unconfirmed_suffix', language)})"
            )
        else:
            unresolved_lines.append(f"▫️ {name_text}")
    add_section(ui("section_unresolved", language), unresolved_lines)
    return lines


def _insufficient_reason_lines(result: VerifyProductResult, language: Language) -> list[str]:
    quality = result.quality
    if quality is None or quality.status != "insufficient":
        return []
    reasons: list[str] = []
    for value in (*quality.reasons, *quality.warnings):
        localized = _localize_quality_message(value, language)
        if localized and localized not in reasons:
            reasons.append(localized)
        if len(reasons) == DEFAULT_MAX_REASONS:
            break
    if not reasons:
        return []
    return ["", ui("why_insufficient", language), *(f"• {item}" for item in reasons)]


def _summary_lines(result: VerifyProductResult, language: Language) -> list[str]:
    identity = result.identity
    category = result.category
    quality = result.quality
    name = " ".join(filter(None, (
        identity.brand if identity else result.request.brand,
        (identity.commercial_model or identity.base_model) if identity else result.request.model,
    )))
    lines = [f"\U0001f4e6 {name}".rstrip()]
    if category is not None:
        confidence = confidence_label(category.confidence, language)
        lines.append(f"{ui('category_label', language)}: {category.category_name} ({confidence})")
    if quality is not None:
        status_label = quality_label(quality.status, language)
        lines.append(f"{ui('quality_label', language)}: {status_label}")
        lines.append(f"{ui('coverage_label', language)}: {quality.coverage_percent}%")
        lines.append(ui(
            "confirmed_summary", language,
            confirmed=quality.confirmed_count, total=quality.schema_total,
            unresolved=quality.unresolved_count, conflicts=quality.conflict_count,
        ))
    if result.served_from_cache:
        age = _format_age(result.cache_age_seconds or 0.0, language)
        lines.append(ui("cache_indicator", language, age=age))
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
    language: Language = DEFAULT_LANGUAGE,
) -> list[str]:
    """Render a VerifyProductResult as one or more Telegram-safe messages."""
    if not result.success:
        return format_error(result, language=language)

    lines = _summary_lines(result, language)
    lines.extend(_insufficient_reason_lines(result, language))
    lines.extend(_status_section_lines(result, max_attributes=max_attributes, language=language))

    return chunk_lines(lines, max_message_length)


# ---------------------------------------------------------------------------
# Stage 13: job-lifecycle formatting.
# ---------------------------------------------------------------------------


def _product_name(request: VerifyProductRequest) -> str:
    return f"{request.brand} {request.model}".strip()


def format_accepted(request: VerifyProductRequest, *, language: Language = DEFAULT_LANGUAGE) -> str:
    """Sent immediately after a new job is created -- the required "принял" ack."""
    return ui("accepted", language, product=_product_name(request))


def format_started(request: VerifyProductRequest, *, language: Language = DEFAULT_LANGUAGE) -> str:
    """Sent once, when a job actually begins running (leaves "queued")."""
    return ui("started", language, product=_product_name(request))


def format_duplicate(job: Job, *, language: Language = DEFAULT_LANGUAGE) -> str:
    """Sent instead of creating a second job for an already-active request."""
    state_label = job_state_label(job.state, language)
    return ui("duplicate", language, product=_product_name(job.request), state=state_label)


def format_status(jobs: list[Job], *, language: Language = DEFAULT_LANGUAGE) -> str:
    """Rendered for /status: a chat's currently active (queued/running) jobs."""
    if not jobs:
        return ui("status_none", language)
    lines = [ui("status_header", language)]
    for job in jobs:
        state_label = job_state_label(job.state, language)
        lines.append(f"• {_product_name(job.request)} — {state_label}")
    return "\n".join(lines)


def format_cancelled(count: int, *, language: Language = DEFAULT_LANGUAGE) -> str:
    """Sent as the immediate reply to /cancel."""
    if count == 0:
        return ui("cancelled_none", language)
    if count == 1:
        return ui("cancelled_one", language)
    return ui("cancelled_many", language, count=count)


def format_job_outcome(job: Job, *, language: Language = DEFAULT_LANGUAGE) -> list[str]:
    """The final message(s) for a job that reached a terminal state.

    A cancelled job intentionally produces no message: the user already got
    an immediate /cancel confirmation, and cooperative cancellation means a
    running computation's result (if it finishes anyway) must be discarded,
    never delivered.
    """
    if job.state == "completed" and job.result is not None:
        return format_result(job.result, language=language)
    if job.state == "failed":
        if job.result is not None:
            return format_result(job.result, language=language)
        return [error_template("internal_error", language)]
    return []
