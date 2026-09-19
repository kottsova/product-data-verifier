"""Telegram text for discovery-only results (Stage 33.1: links for manual checking)."""

from __future__ import annotations

from bot.formatters import DEFAULT_MAX_MESSAGE_LENGTH, chunk_lines
from core.official_documents import OfficialDocument
from services.discovery_debug import DiscoveryDebugResult, DiscoverySource

_DOC_LABELS = {
    "manual": "Manual",
    "user_guide": "User guide",
    "instruction": "Instruction",
    "quick_start_guide": "Quick start guide",
    "datasheet": "Datasheet",
    "spec_sheet": "Spec sheet",
    "safety_document": "Safety document",
    "declaration": "Declaration",
    "certificate": "Certificate",
}
_YES_NO = {"true": "yes", "false": "no", "unknown": "unknown"}


def _flag(value: object) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    return _YES_NO.get(str(value), str(value))


def _page_lines(index: int, item: DiscoverySource) -> list[str]:
    match = "exact" if item.model_match == "exact" else "probable"
    detail = item.reason
    if item.sku_relation == "regional_suffix":
        detail = f"regional SKU suffix '-{item.sku_suffix}' on the requested base model; {detail}"
    return [f"{index}. {item.url}", f"   {match} — {detail}"]


def _document_lines(index: int, item: OfficialDocument) -> list[str]:
    label = _DOC_LABELS.get(item.doc_type, item.doc_type)
    return [
        f"{index}. {label} — {item.url}",
        f"   {item.model_match} · {item.authority} · {item.reason}",
    ]


def _secondary_lines(index: int, item: DiscoverySource) -> list[str]:
    return [f"{index}. {item.url}", f"   {item.model_match} · {item.authority}"]


def _important_rejected(items: tuple[DiscoverySource, ...], limit: int = 5) -> tuple[DiscoverySource, ...]:
    """Show distinct diagnostic failure modes before ordinary search noise."""
    priority = (
        "different commercial-model variant", "conflicting model identifier",
        "search results are not", "accessory or replacement part",
        "obvious_non_product", "brand-only evidence",
    )
    chosen: list[DiscoverySource] = []
    for marker in priority:
        item = next((item for item in items if marker in item.reason.casefold() and item not in chosen), None)
        if item is not None:
            chosen.append(item)
        if len(chosen) >= limit:
            return tuple(chosen)
    for item in items:
        if item not in chosen:
            chosen.append(item)
        if len(chosen) >= limit:
            break
    return tuple(chosen)


def _titled(title: str, count: int) -> list[str]:
    return ["", f"{title} ({count})"]


def format_discovery_result(
    result: DiscoveryDebugResult, *, include_all_rejected: bool = False,
) -> list[str]:
    counts = result.candidate_counts
    pages = result.official_pages or tuple(
        item for item in result.official if item.page_role == "product"
    )
    support = result.support_pages or tuple(
        item for item in result.official if item.page_role == "support"
    )
    lines = [
        f"🔎 {result.product_name}",
        f"Статус: {result.status} · exact official page: {'да' if result.exact_official_found else 'нет'}",
        f"Время discovery: {result.runtime_seconds:.1f} с · search status: {result.search_status}",
        f"Providers: {', '.join(result.providers) or 'нет'}",
        (
            "Trace: "
            f"provider raw={counts.get('provider_raw', 0)}, "
            f"collected={counts.get('collected', 0)}, "
            f"normalized={counts.get('normalized', 0)}, "
            f"unique={counts.get('unique', 0)}, "
            f"accepted={counts.get('accepted', 0)}, "
            f"rejected={counts.get('rejected', 0)}, "
            f"duplicates={counts.get('duplicates', 0)}"
        ),
    ]

    lines.extend(_titled("Official product pages", len(pages)))
    if not pages:
        lines.append("— not found")
    for index, item in enumerate(pages, start=1):
        lines.extend(_page_lines(index, item))

    lines.extend(_titled("Official support pages", len(support)))
    if not support:
        lines.append("— not found")
    for index, item in enumerate(support, start=1):
        lines.append(f"{index}. {item.url}")

    lines.extend(_titled("Official documents", len(result.documents)))
    if not result.documents:
        lines.append("— not found")
    for index, document in enumerate(result.documents, start=1):
        lines.extend(_document_lines(index, document))

    secondary = (*result.dealers, *result.secondary)
    lines.extend(_titled("Secondary", len(secondary)))
    if not secondary:
        lines.append("— not found")
    for index, item in enumerate(secondary[:8], start=1):
        lines.extend(_secondary_lines(index, item))
    if len(secondary) > 8:
        lines.append(f"… ещё {len(secondary) - 8} вторичных источников")

    meta = result.discovery_metadata or {}
    lines.extend(("", "Discovery metadata"))
    if meta.get("inspected_pages"):
        lines.extend((
            f"• expandable specs: {_flag(meta.get('has_expandable_specs'))}",
            f"• hidden spec content: {_flag(meta.get('has_hidden_spec_content'))}",
            f"• interaction required: {_flag(meta.get('requires_interaction'))}",
        ))
    else:
        lines.extend((
            "• expandable specs: unknown (no official product page was inspected)",
            "• hidden spec content: unknown",
            "• interaction required: unknown",
        ))

    rejected = result.rejected if include_all_rejected else _important_rejected(result.rejected)
    lines.extend(("", f"Отклонённые кандидаты ({len(rejected)})"))
    for index, item in enumerate(rejected, start=1):
        lines.extend((f"{index}. {item.url}", f"   {item.reason}"))
    if len(result.rejected) > len(rejected):
        lines.extend((
            "",
            f"Ещё отклонённых: {len(result.rejected) - len(rejected)}. "
            "Команда /discover_rejected покажет полный trace последнего запуска.",
        ))
    if result.provider_failures:
        lines.extend(("", f"Сбои/таймауты providers ({len(result.provider_failures)}):"))
        for failure in result.provider_failures[:12]:
            lines.append(
                f"• {failure['provider']}: {failure['status']} — "
                f"{failure.get('message') or 'без сообщения'}"
            )
        if len(result.provider_failures) > 12:
            lines.append(f"… ещё {len(result.provider_failures) - 12}")
    return chunk_lines(lines, DEFAULT_MAX_MESSAGE_LENGTH)
