"""Stage 33 discovery-only application service.

This boundary deliberately exposes only source discovery: official product
pages, official support pages, official documents (manual, datasheet,
declaration, ...) and discovery metadata about how a page hides its
specifications.  It never extracts, validates, exports or localizes values.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from difflib import SequenceMatcher
from dataclasses import asdict, dataclass, field, replace
import re
import threading
import time
from typing import Callable, Iterable, Literal
from urllib.parse import unquote, urlparse

from core.budget import WallClockBudget
from core.discovery import (
    Candidate,
    DiscoveryOutcome,
    DiscoveryRuntimeConfig,
    ResilientSearchSession,
    _page_kind,
    _registrable_domain,
    _search_result_record,
    canonicalize_url,
    discover_with_status,
)
from core.official_documents import (
    CANONICAL_FIELD,
    OfficialDocument,
    RejectedDocument,
    classify_document,
    document_model_match,
    documents_by_canonical_field,
    extract_documents,
    is_document_index,
    merge_documents,
)
from core.page_inspection import PageInspection, fetch_working_page, inspect_product_page
from core.sku import requested_sku, sku_relation


DiscoveryGroup = Literal["official", "dealer", "secondary", "rejected"]
DisplayMatch = Literal["exact", "probable", "weak", "rejected"]

_DOCUMENT_SUFFIXES = (".pdf", ".doc", ".docx")
_OFFICIAL_SURFACE_PROVIDERS = frozenset({"direct_domain_probe", "browser_official_discovery"})
_MANUAL_TYPES = frozenset({"manual", "user_guide", "instruction", "quick_start_guide"})


@dataclass(frozen=True, slots=True)
class DiscoverySource:
    url: str
    domain: str
    source_type: str
    title: str
    model_match: DisplayMatch
    authority: str
    reason: str
    group: DiscoveryGroup
    sku_relation: str = ""
    sku_suffix: str = ""
    page_role: str = "product"  # product | support | document

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class DiscoveryDebugResult:
    product_name: str
    brand: str
    model: str
    market: str
    status: Literal["PASS", "PARTIAL", "FAIL"]
    exact_official_found: bool
    official: tuple[DiscoverySource, ...]
    dealers: tuple[DiscoverySource, ...]
    secondary: tuple[DiscoverySource, ...]
    rejected: tuple[DiscoverySource, ...]
    runtime_seconds: float
    search_status: str
    attempted_queries: tuple[str, ...]
    providers: tuple[str, ...]
    provider_failures: tuple[dict[str, object], ...]
    candidate_counts: dict[str, int]
    # ---- Stage 33.1 -------------------------------------------------------
    official_pages: tuple[DiscoverySource, ...] = ()
    support_pages: tuple[DiscoverySource, ...] = ()
    documents: tuple[OfficialDocument, ...] = ()
    rejected_documents: tuple[RejectedDocument, ...] = ()
    document_fields: dict[str, list[str]] = field(default_factory=dict)
    page_inspections: tuple[PageInspection, ...] = ()
    discovery_metadata: dict[str, object] = field(default_factory=dict)
    official_paths: tuple[dict[str, object], ...] = ()
    trace: dict[str, object] = field(default_factory=dict)
    # ---- Stage 33.2 -------------------------------------------------------
    page_metadata: tuple[dict[str, object], ...] = ()
    sku_rejections: tuple[dict[str, str], ...] = ()
    performance: dict[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        data = asdict(self)
        for name in ("official", "dealers", "secondary", "rejected", "official_pages", "support_pages"):
            data[name] = [item.to_dict() for item in getattr(self, name)]
        data["documents"] = [item.to_dict() for item in self.documents]
        data["rejected_documents"] = [item.to_dict() for item in self.rejected_documents]
        data["page_inspections"] = [item.to_dict() for item in self.page_inspections]
        return data


def parse_discovery_product_name(text: str) -> tuple[str, str] | None:
    """Resolve a name-only input into a conservative brand/model split.

    Pipe syntax remains available for arbitrary multi-word brands.  Plain text
    handles leading ``The`` brands and ampersand brand names generically; all
    other names follow the bot's established first-token brand convention.
    """
    normalized = " ".join((text or "").split())
    if not normalized:
        return None
    if "|" in normalized:
        parts = [" ".join(part.split()) for part in normalized.split("|")]
        if len(parts) >= 2 and parts[0] and parts[1]:
            return parts[0], parts[1]
        return None
    tokens = normalized.split()
    if len(tokens) < 2:
        return None
    brand_width = 1
    if tokens[0].casefold() == "the" and len(tokens) >= 3:
        brand_width = 2
    elif len(tokens) >= 4 and tokens[1] == "&":
        brand_width = 3
    return " ".join(tokens[:brand_width]), " ".join(tokens[brand_width:])


def _display_match(candidate: Candidate, *, rejected: bool = False) -> DisplayMatch:
    if rejected:
        return "rejected"
    relation = str(candidate.get("relevance_relation") or "")
    if relation == "exact":
        return "exact"
    if relation == "likely_variant":
        return "probable"
    return "weak"


def _authority(candidate: Candidate) -> tuple[str, DiscoveryGroup]:
    source_type = str(candidate.get("source_type") or "other")
    verified = candidate.get("authority_status") == "verified"
    if verified and source_type == "official_document":
        return "official support", "official"
    if verified and source_type == "manufacturer":
        return "manufacturer", "official"
    if verified and source_type == "retailer":
        return "authorized dealer", "dealer"
    return "secondary", "secondary"


def _page_role(url: str, title: str) -> str:
    """Product page, support page, or document -- never mixed."""
    path = urlparse(url).path.lower()
    if path.endswith(_DOCUMENT_SUFFIXES):
        return "document"
    if classify_document(url, title) is not None and not is_document_index(url, title):
        return "document"
    if _page_kind(url) == "support" or is_document_index(url, title):
        return "support"
    return "product"


def _candidate_view(candidate: Candidate, *, rejected: bool = False) -> DiscoverySource:
    authority, group = _authority(candidate)
    if rejected:
        group = "rejected"
    reasons = list(candidate.get("relevance_reasons") or ())
    authority_reason = str(candidate.get("authority_reason") or "").strip()
    if authority_reason:
        reasons.append(authority_reason)
    if not reasons:
        reasons.append(str(candidate.get("product_match_evidence") or "candidate retained"))
    url = str(candidate.get("url") or "")
    title = str(candidate.get("title") or "")
    return DiscoverySource(
        url=url,
        domain=str(candidate.get("domain") or ""),
        source_type=str(candidate.get("source_type") or "other"),
        title=title,
        model_match=_display_match(candidate, rejected=rejected),
        authority=authority,
        reason="; ".join(dict.fromkeys(reason for reason in reasons if reason)),
        group=group,
        sku_relation=str(candidate.get("sku_relation") or ""),
        sku_suffix=str(candidate.get("sku_suffix") or ""),
        page_role=_page_role(url, title),
    )


def _official_hosts(candidates: Iterable[Candidate]) -> list[str]:
    hosts: list[str] = []
    for candidate in candidates:
        if candidate.get("authority_status") == "verified" and candidate.get("source_type") in {
            "manufacturer", "official_document",
        }:
            host = _registrable_domain(str(candidate.get("domain") or ""))
            if host and host not in hosts:
                hosts.append(host)
    return hosts


def _document_from_candidate(
    source: DiscoverySource, brand: str, model: str,
) -> tuple[OfficialDocument | None, RejectedDocument | None]:
    doc_type = classify_document(source.url, source.title)
    if doc_type is None:
        # A PDF is not a document merely because it is a PDF.
        return None, RejectedDocument(
            source.url, "unknown", source.title, "document purpose is not identified (not a manual/datasheet/declaration/...)",
        )
    match, reason = document_model_match(
        model, brand, source.url, source.title, linked_from_exact_page=False,
    )
    if match is None and source.model_match == "exact":
        match, reason = "exact", "search result carries the requested model identifier"
    if match is None:
        return None, RejectedDocument(source.url, doc_type, source.title, reason)
    return OfficialDocument(
        url=source.url,
        doc_type=doc_type,
        canonical_field=CANONICAL_FIELD[doc_type],
        title=source.title or unquote(urlparse(source.url).path.rsplit("/", 1)[-1]),
        authority=source.authority,
        model_match=match,
        reason=f"{reason}; found on verified official domain {source.domain}",
        source_page=source.url,
        found_on=(source.url,),
        file_type="pdf" if urlparse(source.url).path.lower().endswith(".pdf") else "page",
    ), None


def _metadata_from_inspections(
    inspections: list[tuple[DiscoverySource, PageInspection]],
) -> dict[str, object]:
    """Aggregate per-page diagnostics, preferring the exact product page."""
    if not inspections:
        return {
            "inspected_pages": 0, "has_expandable_specs": False,
            "has_hidden_spec_content": "unknown", "requires_interaction": "unknown",
            "primary_page": "",
        }
    ordered = sorted(inspections, key=lambda item: (item[0].model_match != "exact",))
    source, primary = ordered[0]

    def merged(name: str) -> str:
        values = [getattr(inspection, name) for _, inspection in ordered]
        for preferred in ("true", "unknown", "false"):
            if preferred in values:
                return preferred
        return "unknown"

    return {
        "inspected_pages": len(inspections),
        "primary_page": primary.url or source.url,
        "has_expandable_specs": primary.has_expandable_specs,
        "has_hidden_spec_content": primary.has_hidden_spec_content,
        "requires_interaction": primary.requires_interaction,
        "any_page_requires_interaction": merged("requires_interaction"),
        "signals": list(primary.signals),
        "spec_controls": list(primary.spec_controls),
    }


def _result_from_outcome(
    product_name: str,
    brand: str,
    model: str,
    market: str,
    outcome: DiscoveryOutcome,
    runtime_seconds: float,
    *,
    fetch: Callable[[str], tuple[str, str] | None] | None = None,
    extra_results: Iterable[object] = (),
) -> DiscoveryDebugResult:
    grouped: dict[DiscoveryGroup, list[DiscoverySource]] = {
        "official": [], "dealer": [], "secondary": [], "rejected": [],
    }
    for candidate in outcome.candidates:
        view = _candidate_view(candidate)
        grouped[view.group].append(view)
    for candidate in outcome.rejected_candidates:
        grouped["rejected"].append(_candidate_view(candidate, rejected=True))

    known_rejected_urls = {item.url for item in grouped["rejected"]}
    for entry in outcome.trace.entries:
        if entry.outcome != "rejected" or not entry.canonical_url:
            continue
        if entry.canonical_url in known_rejected_urls:
            continue
        known_rejected_urls.add(entry.canonical_url)
        domain = entry.canonical_url.split("/", 3)[2] if "/" in entry.canonical_url else ""
        grouped["rejected"].append(DiscoverySource(
            entry.canonical_url, domain, "other", entry.title, "rejected",
            "secondary", entry.reason, "rejected",
        ))

    official_all = grouped["official"]
    official_pages = [item for item in official_all if item.page_role == "product"]
    support_pages = [item for item in official_all if item.page_role == "support"]
    document_sources = [item for item in official_all if item.page_role == "document"]

    documents: list[OfficialDocument] = []
    rejected_documents: list[RejectedDocument] = []
    for source in document_sources:
        document, rejection = _document_from_candidate(source, brand, model)
        if document is not None:
            documents.append(document)
        if rejection is not None:
            rejected_documents.append(rejection)

    inspections: list[tuple[DiscoverySource, PageInspection]] = []
    if fetch is not None:
        targets: list[tuple[DiscoverySource, str]] = []
        rank = {"exact": 0, "probable": 1, "weak": 2}
        for source in sorted(official_pages, key=lambda item: rank.get(item.model_match, 3))[:6]:
            targets.append((source, "product"))
        for source in [*support_pages, *document_sources]:
            if len([t for t in targets if t[1] == "scan"]) >= 3:
                break
            if not source.url.lower().split("?")[0].endswith(_DOCUMENT_SUFFIXES):
                targets.append((source, "scan"))

        def load(target: tuple[DiscoverySource, str]):
            return target, fetch(target[0].url)

        with ThreadPoolExecutor(max_workers=max(1, min(4, len(targets)))) as pool:
            fetched = list(pool.map(load, targets)) if targets else []
        official_hosts = _official_hosts(outcome.candidates)
        for (source, kind), page in fetched:
            if page is None:
                continue
            final_url, html = page
            if kind == "product":
                inspections.append((source, inspect_product_page(html, final_url)))
            title_match = re.search(r"<title[^>]*>(.*?)</title>", html[:200_000], re.I | re.S)
            page_title = " ".join((title_match.group(1) if title_match else "").split())
            found, rejected_links = extract_documents(
                html, final_url, brand=brand, model=model,
                source_authority="official (linked from verified official page)",
                source_is_exact_page=(source.model_match == "exact" and kind == "product"),
                source_is_probable_page=(source.model_match in {"exact", "probable"}),
                official_hosts=official_hosts, page_title=page_title,
            )
            documents.extend(found)
            rejected_documents.extend(rejected_links)
        # Links are for manual checking: show the spelling that actually loads
        # (canonicalisation drops trailing slashes some sites 404 without).
        working = {source.url: page[0] for (source, _), page in fetched if page}
        official_pages = [replace(item, url=working.get(item.url, item.url)) for item in official_pages]
        support_pages = [replace(item, url=working.get(item.url, item.url)) for item in support_pages]

    # Document links surfaced by dedicated document queries.
    official_hosts = _official_hosts(outcome.candidates)
    for record in extra_results:
        url = str(getattr(record, "url", "") or "")
        title = str(getattr(record, "title", "") or "")
        canonical = canonicalize_url(url)
        if not canonical:
            continue
        doc_type = classify_document(url, title)
        if doc_type is None:
            continue
        host_root = _registrable_domain(urlparse(canonical).hostname or "")
        if host_root not in official_hosts:
            rejected_documents.append(RejectedDocument(
                canonical, doc_type, title, "document host is not a verified official domain",
            ))
            continue
        match, reason = document_model_match(model, brand, canonical, title, linked_from_exact_page=False)
        if match is None:
            rejected_documents.append(RejectedDocument(canonical, doc_type, title, reason))
            continue
        documents.append(OfficialDocument(
            url=canonical, doc_type=doc_type, canonical_field=CANONICAL_FIELD[doc_type],
            title=title or canonical, authority="official (verified manufacturer domain)",
            model_match=match, reason=f"{reason}; returned by a document-focused query on an official domain",
            source_page=canonical, found_on=(canonical,),
            file_type="pdf" if urlparse(canonical).path.lower().endswith(".pdf") else "page",
        ))

    merged_documents = merge_documents(documents)
    exact_official = any(item.model_match == "exact" for item in official_pages)
    if exact_official:
        status: Literal["PASS", "PARTIAL", "FAIL"] = "PASS"
    elif official_all or merged_documents or any(item.model_match == "exact" for item in grouped["secondary"]):
        status = "PARTIAL"
    else:
        status = "FAIL"

    attempts = outcome.provider_attempts
    providers = tuple(dict.fromkeys(item.provider for item in attempts))
    failures = tuple({
        "provider": item.provider,
        "query": item.query,
        "status": item.status,
        "message": item.message,
        "duration_seconds": item.duration_seconds,
    } for item in attempts if item.status != "success")
    trace = outcome.trace
    counts = {
        "provider_raw": trace.provider_raw_result_count,
        "collected": trace.collected_result_count,
        "normalized": trace.normalized_result_count,
        "unique": trace.unique_candidate_count,
        "duplicates": trace.duplicate_count,
        "accepted": trace.accepted_candidate_count,
        "rejected": trace.rejected_candidate_count,
        "official": len(official_all),
        "dealer": len(grouped["dealer"]),
        "secondary": len(grouped["secondary"]),
        "documents": len(merged_documents),
    }
    return DiscoveryDebugResult(
        product_name=product_name,
        brand=brand,
        model=model,
        market=market,
        status=status,
        exact_official_found=exact_official,
        official=tuple([*official_pages, *support_pages]),
        dealers=tuple(grouped["dealer"]),
        secondary=tuple(grouped["secondary"]),
        rejected=tuple(grouped["rejected"]),
        runtime_seconds=round(runtime_seconds, 3),
        search_status=outcome.search_status,
        attempted_queries=tuple(outcome.attempted_queries),
        providers=providers,
        provider_failures=failures,
        candidate_counts=counts,
        official_pages=tuple(official_pages),
        support_pages=tuple(support_pages),
        documents=tuple(merged_documents),
        rejected_documents=tuple(rejected_documents[:40]),
        document_fields=documents_by_canonical_field(merged_documents),
        page_inspections=tuple(inspection for _, inspection in inspections),
        discovery_metadata=_metadata_from_inspections(inspections),
        official_paths=_official_paths(outcome),
        trace=_trace_payload(outcome, grouped, merged_documents, rejected_documents),
        page_metadata=tuple(_page_metadata(source, inspection) for source, inspection in inspections),
        sku_rejections=_sku_rejections(model, grouped["rejected"], rejected_documents),
        performance=_performance(outcome, counts, runtime_seconds),
    )


_REGION_PATH = re.compile(r"/([a-z]{2})[-_]([a-z]{2})(?:/|$)", re.I)
_LANG_PATH = re.compile(r"^/([a-z]{2})(?:/|$)", re.I)


def locale_region(url: str) -> str:
    """Best-effort locale/region label from the URL alone (path locale, else host TLD)."""
    parsed = urlparse(url)
    match = _REGION_PATH.search(parsed.path)
    if match:
        return f"{match.group(1).lower()}-{match.group(2).upper()}"
    labels = (parsed.hostname or "").lower().split(".")
    if len(labels) >= 2:
        tld = labels[-1]
        if len(tld) == 2:
            return f".{tld}" + (f" ({_LANG_PATH.match(parsed.path).group(1).lower()})" if _LANG_PATH.match(parsed.path) else "")
        if len(labels) >= 3 and labels[-2] in {"global", "eu", "cee", "asia"}:
            return labels[-2]
        return "global (.%s)" % tld
    return "unknown"


def _page_metadata(source: DiscoverySource, inspection: PageInspection) -> dict[str, object]:
    return {
        "url": inspection.url or source.url,
        "domain": source.domain,
        "locale_region": locale_region(inspection.url or source.url),
        "model_match": source.model_match,
        "expandable_specs": inspection.has_expandable_specs,
        "hidden_spec_content": inspection.has_hidden_spec_content,
        "interaction_required": inspection.requires_interaction,
        "spec_location": inspection.spec_location,
        "spec_controls": list(inspection.spec_controls),
        "signals": list(inspection.signals),
        "spec_rows_visible": inspection.spec_rows_visible,
        "spec_rows_hidden": inspection.spec_rows_hidden,
        "json_spec_blocks": inspection.json_spec_blocks,
    }


_ID_TOKEN = re.compile(r"[A-Za-z0-9]+(?:[-_/][A-Za-z0-9]+)*")


def _neighbour_sku(base: str, text: str) -> str:
    """An identifier in ``text`` that looks like a sibling of ``base`` (same shape, not equal)."""
    wanted = base.upper()
    for token in _ID_TOKEN.findall(text):
        compact = re.sub(r"[^A-Za-z0-9]", "", token).upper()
        if compact == wanted or len(compact) < 5 or abs(len(compact) - len(wanted)) > 2:
            continue
        if not (re.search(r"[A-Z]", compact) and re.search(r"\d", compact)):
            continue
        if SequenceMatcher(None, compact, wanted).ratio() >= 0.75:
            return token
    return ""


def _sku_rejections(
    model: str,
    rejected: list[DiscoverySource],
    rejected_documents: list[RejectedDocument],
) -> tuple[dict[str, str], ...]:
    """Near-miss candidates rejected because they carry a different SKU/variant."""
    sku = requested_sku(model)
    if sku is None:
        return ()
    requested = f"{sku.base_display}/{sku.suffix}" if sku.suffix else sku.base_display
    found: list[dict[str, str]] = []
    seen: set[str] = set()
    items = [(item.url, item.title, item.reason) for item in rejected]
    items += [(doc.url, doc.title, doc.reason) for doc in rejected_documents]
    for url, title, reason in items:
        if url in seen:
            continue
        relation = sku_relation(model, url, title)
        if relation.kind in {"different_suffix", "different_variant", "base_only"}:
            seen.add(url)
            found.append({
                "url": url, "found_sku": relation.evidence, "requested_sku": requested,
                "relation": relation.kind, "reason": reason,
            })
            continue
        neighbour = _neighbour_sku(sku.base, f"{unquote(url)} {title}")
        if neighbour and relation.kind == "absent":
            seen.add(url)
            found.append({
                "url": url, "found_sku": neighbour, "requested_sku": requested,
                "relation": "neighbour_sku", "reason": reason,
            })
    return tuple(found[:30])


def _performance(outcome: DiscoveryOutcome, counts: dict[str, int], runtime: float) -> dict[str, object]:
    statuses: dict[str, int] = {}
    for attempt in outcome.provider_attempts:
        statuses[attempt.status] = statuses.get(attempt.status, 0) + 1
    methods: dict[str, int] = {}
    for attempt in outcome.provider_attempts:
        if attempt.provider == "direct_domain_probe":
            for method, count in attempt.method_requests:
                methods[method] = max(methods.get(method, 0), int(count))
    first_path = next(
        (f"{attempt.provider}:{attempt.discovery_method}"
         for attempt in outcome.provider_attempts if attempt.discovery_method),
        "",
    )
    return {
        "first_official_path": first_path,
        "probe_requests": sum(methods.values()),
        "domain_probes": methods.get("domain_resolution", 0),
        "probe_methods": methods,
        "runtime_seconds": round(runtime, 1),
        "slow": "over_60s" if runtime > 60 else "over_30s" if runtime > 30 else "",
        "query_count": len(outcome.attempted_queries),
        "provider_attempts": len(outcome.provider_attempts),
        "raw_candidates": counts.get("provider_raw", 0),
        "unique_candidates": counts.get("unique", 0),
        "accepted": counts.get("accepted", 0),
        "rejected": counts.get("rejected", 0),
        "blocked": statuses.get("blocked", 0),
        "timeout": statuses.get("timeout", 0),
        "circuit_open": statuses.get("circuit_open", 0),
        "attempt_statuses": statuses,
    }


def _official_paths(outcome: DiscoveryOutcome) -> tuple[dict[str, object], ...]:
    """Which generic official-discovery paths ran, and what each produced."""
    paths: dict[str, dict[str, object]] = {}
    for attempt in outcome.provider_attempts:
        if attempt.provider not in {"direct_domain_probe", "browser_official_discovery"}:
            continue
        for method, count in attempt.method_requests:
            entry = paths.setdefault(
                f"{attempt.provider}:{method}",
                {"path": f"{attempt.provider}:{method}", "requests": 0, "queries": 0},
            )
            entry["requests"] = max(int(entry["requests"]), int(count))
            entry["queries"] = int(entry["queries"]) + 1
    for path in ("serp_identity_queries", "serp_domain_restricted_queries", "serp_official_bootstrap"):
        paths.setdefault(path, {"path": path, "requests": 0, "queries": 0})
    for query in outcome.attempted_queries:
        key = (
            "serp_domain_restricted_queries" if "site:" in query
            else "serp_official_bootstrap" if "official" in query
            else "serp_identity_queries"
        )
        paths[key]["queries"] = int(paths[key]["queries"]) + 1
    return tuple(paths.values())


def _trace_payload(
    outcome: DiscoveryOutcome,
    grouped: dict[DiscoveryGroup, list[DiscoverySource]],
    documents: list[OfficialDocument],
    rejected_documents: list[RejectedDocument],
) -> dict[str, object]:
    trace = outcome.trace
    return {
        "queries": list(outcome.attempted_queries),
        "provider_attempts": [asdict(item) for item in outcome.provider_attempts],
        "raw_and_normalized": [asdict(entry) for entry in trace.entries],
        "accepted": [item.to_dict() for group in ("official", "dealer", "secondary") for item in grouped[group]],  # type: ignore[index]
        "rejected": [item.to_dict() for item in grouped["rejected"]],
        "documents": [item.to_dict() for item in documents],
        "rejected_documents": [item.to_dict() for item in rejected_documents],
        "provider_errors": [
            {"provider": item.provider, "query": item.query, "status": item.status, "message": item.message}
            for item in outcome.provider_attempts if item.status in {"blocked", "timeout", "error", "parse_error"}
        ],
    }


def document_queries(brand: str, model: str, domains: list[str]) -> list[str]:
    """Document-focused queries, restricted to verified official domains."""
    sku = requested_sku(model)
    key = sku.base_display if sku is not None else model
    queries: list[str] = []
    for domain in domains[:2]:
        queries.append(f'"{key}" manual site:{domain}')
    if not domains:
        queries.append(f'{brand} "{key}" user manual pdf')
    queries.append(f'{brand} "{key}" declaration of conformity')
    return queries


class DiscoveryDebugService:
    """Synchronous discovery service shared by Telegram and diagnostics."""

    def __init__(
        self, *, wall_clock_budget_seconds: float = 75.0,
        providers: Callable[[], Iterable[object]] | None = None,
    ) -> None:
        if wall_clock_budget_seconds <= 0:
            raise ValueError("wall_clock_budget_seconds must be positive")
        self.wall_clock_budget_seconds = float(wall_clock_budget_seconds)
        # Diagnostics only: restrict the provider chain (e.g. official-only, no SERP).
        self._providers = providers
        self._last_by_chat: dict[int, DiscoveryDebugResult] = {}
        self._lock = threading.Lock()

    def discover_name(
        self, product_name: str, *, market: str = "global", chat_id: int | None = None,
    ) -> DiscoveryDebugResult:
        identity = parse_discovery_product_name(product_name)
        if identity is None:
            raise ValueError("brand and model are required")
        brand, model = identity
        name = " ".join(product_name.split())
        started = time.monotonic()
        budget = WallClockBudget(self.wall_clock_budget_seconds)
        runtime = DiscoveryRuntimeConfig()
        page_cache: dict[str, tuple[str, str] | None] = {}

        def fetch(url: str) -> tuple[str, str] | None:
            if url not in page_cache:
                page_cache[url] = fetch_working_page(url, timeout=12.0)
            return page_cache[url]

        provider_chain = list(self._providers()) if self._providers is not None else None
        with ResilientSearchSession(
            market, provider_chain, config=runtime, budget=budget,
        ) as session:
            outcome = discover_with_status(
                brand, model, market=market, searcher=session.search_with_status,
                early_official_stop=True,
            )
            result = _result_from_outcome(
                name, brand, model, market, outcome, time.monotonic() - started, fetch=fetch,
            )
            # Documents linked from the official pages are the cheap, reliable
            # source.  Search-engine document queries only run when they did
            # not already produce a manual-type document.
            if not any(doc.doc_type in _MANUAL_TYPES for doc in result.documents):
                extra: list[object] = []
                attempted = list(outcome.attempted_queries)
                attempts = list(outcome.provider_attempts)
                for query in document_queries(brand, model, _official_hosts(outcome.candidates)):
                    if budget.remaining_seconds < 8:
                        break
                    attempted.append(query)
                    try:
                        response = session.search_with_status(
                            query, skip_providers=_OFFICIAL_SURFACE_PROVIDERS,
                        )
                    except Exception:  # noqa: BLE001 - a failed query is reported via attempts
                        continue
                    attempts.extend(response.attempts)
                    extra.extend(response.results)
                if extra or len(attempted) != len(outcome.attempted_queries):
                    outcome = DiscoveryOutcome(
                        candidates=outcome.candidates, search_status=outcome.search_status,
                        queries=outcome.queries, attempted_queries=attempted, issues=outcome.issues,
                        provider_attempts=attempts, rejected_candidates=outcome.rejected_candidates,
                        trace=outcome.trace,
                    )
                    result = _result_from_outcome(
                        name, brand, model, market, outcome, time.monotonic() - started,
                        fetch=fetch, extra_results=[_search_result_record(item) for item in extra],
                    )
        if chat_id is not None:
            with self._lock:
                self._last_by_chat[chat_id] = result
        return result

    def last_result(self, chat_id: int) -> DiscoveryDebugResult | None:
        with self._lock:
            return self._last_by_chat.get(chat_id)
