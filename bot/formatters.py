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

from bot.attribute_filter import effective_status, filter_user_facing, has_usable_value
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
from bot.localize import category_name, localize_value
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
DEFAULT_MAX_ATTRIBUTES = 250
DEFAULT_MAX_REASONS = 2
DEFAULT_MAX_SOURCES = 6

PRIORITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3}

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


def format_attribute_value(
    value: object,
    unit: str | None,
    *,
    canonical_name: str = "",
    language: Language = "en",
) -> str:
    """Value + unit as text; in RU mode units/prose are localized (see bot.localize)."""
    if value is None:
        return "—"
    if isinstance(value, dict) and {"height", "width", "depth", "unit"} <= value.keys():
        text = f"{value['height']} × {value['width']} × {value['depth']} {value['unit']}".strip()
    else:
        text = str(value)
        if unit and not text.casefold().rstrip().endswith(str(unit).casefold()):
            text = f"{text} {unit}"
    return localize_value(canonical_name, text, unit, language)


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


def ordered_sources(result: VerifyProductResult) -> list[tuple[str, bool]]:
    """(url, is_official) for every source behind a displayed Confirmed value.

    Official sources come first, secondary sources after; inside each group the
    source backing more displayed attributes comes first, then first-seen order.
    """
    counts: dict[str, int] = {}
    official: dict[str, bool] = {}
    for attribute in filter_user_facing(result.attributes):
        if effective_status(attribute) != "Confirmed":
            continue
        for evidence in attribute.supporting_sources:
            if not evidence.source:
                continue
            counts[evidence.source] = counts.get(evidence.source, 0) + 1
            official[evidence.source] = (
                official.get(evidence.source, False)
                or evidence.source_type in _OFFICIAL_SOURCE_TYPES
            )
    order = {url: index for index, url in enumerate(counts)}
    return sorted(
        ((url, official[url]) for url in counts),
        key=lambda item: (not item[1], -counts[item[0]], order[item[0]]),
    )


def _source_lines(result: VerifyProductResult, language: Language) -> list[str]:
    sources = ordered_sources(result)
    if not sources:
        return []
    lines = [f"{ui('section_sources', language)} ({len(sources)}):"]
    for index, (url, is_official) in enumerate(sources[:DEFAULT_MAX_SOURCES], start=1):
        kind = ui("official_source" if is_official else "secondary_source", language)
        lines.append(f"{index}. {kind}: {url}")
    if len(sources) > DEFAULT_MAX_SOURCES:
        lines.append(ui("more_items", language, count=len(sources) - DEFAULT_MAX_SOURCES))
    return lines


def product_image_records(result: VerifyProductResult) -> list[tuple[str, bool, str]]:
    """(url, is_official, source_page) product photos recorded by the pipeline.

    Official photos come first; a record without an explicit role is treated
    as official (results cached before Stage 31.5 only ever stored official
    photos). Deduplicated, https only.
    """
    metadata = result.metadata or {}
    records = metadata.get("product_image_records")
    found: list[tuple[str, bool, str]] = []
    if isinstance(records, (list, tuple)):
        for item in records:
            if isinstance(item, dict) and isinstance(item.get("url"), str):
                found.append((item["url"], item.get("role") != "secondary", str(item.get("source") or "")))
    else:
        images = metadata.get("product_images")
        if isinstance(images, (list, tuple)):
            found = [(url, True, "") for url in images if isinstance(url, str)]
    seen: set[str] = set()
    unique: list[tuple[str, bool, str]] = []
    for url, official, source in found:
        if url.startswith("https://") and url not in seen:
            seen.add(url)
            unique.append((url, official, source))
    return sorted(unique, key=lambda item: not item[1])


def product_image_urls(result: VerifyProductResult) -> list[str]:
    """Safe product photo URLs (official first, then secondary; may be empty)."""
    return [url for url, _, _ in product_image_records(result)]


_AUXILIARY_ORDER = ("review", "opinions", "compare", "pictures", "prices")


def format_auxiliary(
    result: VerifyProductResult, *, language: Language = DEFAULT_LANGUAGE,
) -> tuple[str, str | None] | None:
    """Secondary-source rich card: (text, url Telegram should preview) or None.

    The official source owns spec priority; the secondary reference page keeps
    its user-facing extras (rich preview, review, opinions, compare, pictures,
    prices; site-wide indexes such as "Videos"/"Reviews" are not product links). "Related devices" is never surfaced.
    """
    metadata = result.metadata or {}
    links = [
        item for item in (metadata.get("auxiliary_links") or ())
        if isinstance(item, dict) and str(item.get("url", "")).startswith("https://")
    ]
    page = next((str(item["source"]) for item in links if item.get("source")), None)
    if page is None:
        page = next((url for url, official in ordered_sources(result) if not official), None)
    if page is None:
        return None
    lines = [ui("section_auxiliary", language, kind=ui("secondary_source", language)), page]
    by_kind = {str(item.get("kind")): str(item["url"]) for item in links}
    for kind in _AUXILIARY_ORDER:
        if kind in by_kind:
            lines.append(f"{ui('aux_' + kind, language)}: {by_kind[kind]}")
    return "\n".join(lines), page


def _localized_name(attribute: ServiceAttribute, language: Language) -> str:
    return display_name(attribute.canonical_name, language, fallback=attribute.display_name)


def _attribute_line(attribute: ServiceAttribute, language: Language) -> str:
    """``Name = Value`` for a confirmed attribute; callers only pass items
    already filtered to ``effective_status(item) == "Confirmed"``, so the value
    is guaranteed usable (see bot.attribute_filter.has_usable_value)."""
    name = _localized_name(attribute, language)
    value_text = format_attribute_value(
        attribute.value, attribute.unit,
        canonical_name=attribute.canonical_name, language=language,
    )
    return f"{name} = {value_text}"


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


def found_counts(result: VerifyProductResult) -> tuple[int, int]:
    """(found, total): confirmed user-facing attributes over user-facing attributes.

    The Telegram preview and the wide CSV both derive from
    ``filter_user_facing``, so this equals the number of populated canonical
    columns in the export.
    """
    attributes = filter_user_facing(result.attributes)
    return sum(effective_status(item) == "Confirmed" for item in attributes), len(attributes)


def _attribute_lines(
    result: VerifyProductResult, *, max_attributes: int, language: Language,
) -> list[str]:
    confirmed = _sorted_attributes([
        item for item in filter_user_facing(result.attributes)
        if effective_status(item) == "Confirmed"
    ])
    limit = max(0, max_attributes)
    lines = [_attribute_line(item, language) for item in confirmed[:limit]]
    if len(confirmed) > limit:
        lines.append(ui("more_items", language, count=len(confirmed) - limit))
    return lines


def _conflict_warning_lines(result: VerifyProductResult, language: Language) -> list[str]:
    attributes = filter_user_facing(result.attributes)
    shown = {item.canonical_name: item for item in attributes}
    names = [name for name in dict.fromkeys(result.conflicts) if name in shown]
    for item in attributes:
        if effective_status(item) == "Conflict" and item.canonical_name not in names:
            names.append(item.canonical_name)
    if not names:
        # Conflicting evidence on a field that is not itself shown (for
        # example an identity code) still has to be visible, once.
        if result.quality is not None and result.quality.status == "conflicted":
            return [quality_label("conflicted", language)]
        return []
    labels = [_localized_name(shown[name], language) for name in names[:4]]
    if len(names) > 4:
        labels.append(f"+{len(names) - 4}")
    return [ui("conflicts_warning", language, fields=", ".join(labels))]


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
    name = " ".join(filter(None, (
        identity.brand if identity else result.request.brand,
        (identity.commercial_model or identity.base_model) if identity else result.request.model,
    )))
    lines = [f"\U0001f4e6 {name}".rstrip()]
    if category is not None:
        lines.append(category_name(category.category_name, language))
    found, total = found_counts(result)
    found_line = ui("found_summary", language, found=found, total=total)
    if result.quality is not None and result.quality.status != "conflicted":
        found_line = f"{found_line} · {quality_label(result.quality.status, language)}"
    lines.append(found_line)
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
    lines.extend(_source_lines(result, language))
    lines.extend(_insufficient_reason_lines(result, language))
    attributes = _attribute_lines(result, max_attributes=max_attributes, language=language)
    if attributes:
        lines.extend(("", *attributes))
    warning = _conflict_warning_lines(result, language)
    if warning:
        lines.extend(("", *warning))

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
