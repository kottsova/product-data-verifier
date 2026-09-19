"""Stage 33.0 Telegram text for discovery-only results."""

from __future__ import annotations

from bot.formatters import DEFAULT_MAX_MESSAGE_LENGTH, chunk_lines
from services.discovery_debug import DiscoveryDebugResult, DiscoverySource


def _source_lines(index: int, item: DiscoverySource) -> list[str]:
    title = item.title or "(title отсутствует)"
    return [
        f"{index}. {title}",
        item.url,
        f"домен: {item.domain}",
        f"тип: {item.source_type} · match: {item.model_match} · authority: {item.authority}",
        f"принято: {item.reason}",
    ]


def _section(title: str, items: tuple[DiscoverySource, ...]) -> list[str]:
    lines = ["", f"{title} ({len(items)})"]
    if not items:
        lines.append("— не найдено")
        return lines
    for index, item in enumerate(items, start=1):
        lines.extend(_source_lines(index, item))
    return lines


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


def format_discovery_result(
    result: DiscoveryDebugResult, *, include_all_rejected: bool = False,
) -> list[str]:
    counts = result.candidate_counts
    lines = [
        f"🔎 Stage 33.0 — {result.product_name}",
        f"Статус: {result.status} · exact official: {'да' if result.exact_official_found else 'нет'}",
        f"Время discovery: {result.runtime_seconds:.3f} с · search status: {result.search_status}",
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
    lines.extend(_section("Официальные источники", result.official))
    lines.extend(_section("Авторизованные / официальные дилеры", result.dealers))
    lines.extend(_section("Вторичные источники", result.secondary))
    rejected = result.rejected if include_all_rejected else _important_rejected(result.rejected)
    lines.extend(_section("Отклонённые кандидаты", tuple(rejected)))
    if len(result.rejected) > len(rejected):
        lines.extend((
            "",
            f"Ещё отклонённых: {len(result.rejected) - len(rejected)}. "
            "Команда /discover_rejected покажет полный trace последнего запуска.",
        ))
    if result.provider_failures:
        lines.extend(("", f"Сбои/таймауты providers ({len(result.provider_failures)}):"))
        for failure in result.provider_failures:
            lines.append(
                f"• {failure['provider']}: {failure['status']} — "
                f"{failure.get('message') or 'без сообщения'}"
            )
    return chunk_lines(lines, DEFAULT_MAX_MESSAGE_LENGTH)
