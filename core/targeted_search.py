"""Bounded Stage 5 search for candidate evidence requested by gap analysis."""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Callable, Iterable, Literal, Mapping

from core.discovery import (
    Candidate,
    DiscoveryIssue,
    DiscoveryOutcome,
    GoogleSearchSession,
    Searcher,
    canonicalize_url,
    discover_identity_query_with_status,
)
from core.extract import RawAttribute, extract_attributes
from core.fetch import FetchResult, fetch_candidate
from core.gaps import Gap, GapAnalysisResult, RELATED_FIELDS
from core.identity import ProductIdentity
from core.mapping import CanonicalAttribute, map_attributes


FieldSearchStatus = Literal["success", "unresolved", "blocked", "error"]
StopReason = Literal[
    "strong_official_evidence",
    "query_limit",
    "candidate_limit",
    "discovery_blocked",
    "discovery_error",
    "no_queries",
]
DiscoveryRunner = Callable[[ProductIdentity, str], DiscoveryOutcome]
Fetcher = Callable[[Mapping[str, object]], FetchResult]
Extractor = Callable[[dict[str, object]], list[RawAttribute]]


@dataclass(frozen=True, slots=True)
class TargetedSearchConfig:
    max_queries_per_field: int = 3
    max_candidates_per_query: int = 5
    max_candidates_per_field: int = 8
    stop_on_strong_official_evidence: bool = True

    def __post_init__(self) -> None:
        for name in (
            "max_queries_per_field", "max_candidates_per_query", "max_candidates_per_field",
        ):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} must be positive")


@dataclass(frozen=True, slots=True)
class TargetedQuery:
    canonical_name: str
    search_intent: str
    query: str
    identity_signals: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TargetedSearchPlan:
    category: str
    queries: list[TargetedQuery] = field(default_factory=list)
    deferred_gaps: list[Gap] = field(default_factory=list)
    config: TargetedSearchConfig = field(default_factory=TargetedSearchConfig)

    @property
    def by_field(self) -> dict[str, list[TargetedQuery]]:
        grouped: dict[str, list[TargetedQuery]] = {}
        for query in self.queries:
            grouped.setdefault(query.canonical_name, []).append(query)
        return grouped


@dataclass(frozen=True, slots=True)
class CandidateRejection:
    candidate: Mapping[str, object]
    reason: str


@dataclass(frozen=True, slots=True)
class TargetedQueryResult:
    requested_canonical_field: str
    query: str
    query_intent: str
    discovery_candidates: tuple[Mapping[str, object], ...] = ()
    fetched_sources: tuple[FetchResult, ...] = ()
    extracted_relevant_facts: tuple[RawAttribute, ...] = ()
    mapped_candidate_facts: tuple[CanonicalAttribute, ...] = ()
    related_candidate_facts: tuple[CanonicalAttribute, ...] = ()
    rejected_candidates: tuple[CandidateRejection, ...] = ()
    discovery_status: str = "success"
    issues: tuple[DiscoveryIssue, ...] = ()
    useful_evidence_found: bool = False


@dataclass(frozen=True, slots=True)
class TargetedFieldResult:
    gap: Gap
    query_results: tuple[TargetedQueryResult, ...]
    search_status: FieldSearchStatus
    useful_evidence_found: bool
    stop_reason: StopReason

    @property
    def mapped_candidate_facts(self) -> tuple[CanonicalAttribute, ...]:
        return tuple(
            fact for result in self.query_results for fact in result.mapped_candidate_facts
        )


@dataclass(frozen=True, slots=True)
class TargetedSearchResult:
    plan: TargetedSearchPlan
    fields: list[TargetedFieldResult] = field(default_factory=list)
    fetched_urls: tuple[str, ...] = ()
    fetch_count: int = 0

    @property
    def blocked(self) -> bool:
        return any(field.search_status == "blocked" for field in self.fields)


def _ascii_aliases(gap: Gap) -> list[str]:
    aliases: list[str] = []
    for value in (gap.suggested_search_intent, *gap.aliases):
        value = " ".join((value or "").split())
        if not value or not value.isascii() or len(value.split()) > 6:
            continue
        normalized = value.casefold()
        if normalized in {"weight", "dimensions", "dimension", "size", "color", "colour"}:
            continue
        if normalized not in {item.casefold() for item in aliases}:
            aliases.append(value)
    return aliases or [gap.canonical_name.replace("_", " ")]


def _identity_prefix(identity: ProductIdentity, gap: Gap) -> tuple[str, tuple[str, ...]]:
    model = identity.commercial_model or identity.base_model
    if not model:
        raise ValueError("identity must contain a commercial_model or base_model")
    parts = [identity.brand, model]
    signals = [f"brand={identity.brand}", f"model={model}"]
    if gap.attribute_scope == "variant_level":
        variant = (
            identity.manufacturer_article
            or identity.product_code
            or identity.sku
            or identity.gtin
            or identity.variant_suffix
        )
        if variant and variant.casefold() not in " ".join(parts).casefold():
            parts.append(variant)
            signals.append(f"variant={variant}")
    return " ".join(dict.fromkeys(parts)), tuple(signals)


def build_targeted_search_plan(
    analysis: GapAnalysisResult,
    identity: ProductIdentity,
    *,
    config: TargetedSearchConfig | None = None,
) -> TargetedSearchPlan:
    """Generate a small canonical-field-specific query set for eligible gaps."""
    active_config = config or TargetedSearchConfig()
    queries: list[TargetedQuery] = []
    deferred: list[Gap] = []
    for gap in analysis.gaps:
        if not gap.targeted_search_allowed:
            if gap.gap_state != "satisfied":
                deferred.append(gap)
            continue
        prefix, signals = _identity_prefix(identity, gap)
        terms = _ascii_aliases(gap)
        field_queries: list[tuple[str, str, tuple[str, ...]]] = []
        for term in terms:
            field_queries.append((term, f"{prefix} {term}", signals))

        secondary = (
            identity.manufacturer_article
            or identity.product_code
            or identity.gtin
        )
        if secondary and secondary.casefold() not in prefix.casefold():
            field_queries.append((
                terms[0],
                f"{prefix} {secondary} {terms[0]}",
                (*signals, f"secondary={secondary}"),
            ))
        deduplicated: list[tuple[str, str, tuple[str, ...]]] = []
        seen_queries: set[str] = set()
        for item in field_queries:
            if item[1] not in seen_queries:
                seen_queries.add(item[1])
                deduplicated.append(item)
        queries.extend(
            TargetedQuery(gap.canonical_name, term, query, query_signals)
            for term, query, query_signals
            in deduplicated[:active_config.max_queries_per_field]
        )
    return TargetedSearchPlan(analysis.category, queries, deferred, active_config)


def _related_names(canonical_name: str) -> frozenset[str]:
    return next(
        (family for family in RELATED_FIELDS if canonical_name in family),
        frozenset({canonical_name}),
    )


def _candidate_identity_rejection(
    candidate: Mapping[str, object],
    gap: Gap,
    identity: ProductIdentity,
) -> str | None:
    relation = str(candidate.get("identity_relation") or "unknown")
    relevance = str(candidate.get("model_relevance") or "unknown")
    model_match = str(candidate.get("model_match") or "unknown")
    if relation == "different_model" or relevance == "different_model" or model_match == "mismatch":
        return "Candidate is for a different model."
    if relation == "unknown" and relevance not in {
        "exact_base_model", "variant_of_base_model", "probable_base_model",
    }:
        return "Candidate has no sufficient identity relation to the requested model."
    if gap.attribute_scope != "variant_level":
        return None

    title = f"{candidate.get('title') or ''} {candidate.get('url') or ''}"
    pair = re.search(
        r"(?<!\d)(\d{1,2})\s*(?:GB\s*)?(?:\+|/)\s*(\d{2,4})\s*(?:GB)?(?!\d)",
        title,
        re.IGNORECASE,
    )
    if pair and identity.configuration:
        candidate_config = {"ram": pair.group(1), "storage": pair.group(2)}
        for name, expected in identity.configuration.items():
            expected_number = re.sub(r"\D", "", expected)
            if name in candidate_config and expected_number != candidate_config[name]:
                return f"Candidate has an incompatible {name} configuration."
    if relation == "exact_variant":
        return None
    evidence = " ".join(map(str, candidate.get("identity_verification_evidence") or ())).casefold()
    expected_signals = [
        value for value in (
            identity.variant_suffix,
            identity.manufacturer_article,
            identity.product_code,
            identity.sku,
            identity.gtin,
            identity.configuration.get(gap.canonical_name),
            identity.color if gap.canonical_name == "color" else None,
        ) if value
    ]
    if expected_signals and not any(value.casefold() in evidence for value in expected_signals):
        return "Variant-sensitive field lacks matching variant evidence on the candidate."
    return None


def _relevant_raw(
    requested: str,
    raw: Iterable[RawAttribute],
    exact: Iterable[CanonicalAttribute],
    related: Iterable[CanonicalAttribute],
    ambiguous: Iterable[object],
) -> tuple[RawAttribute, ...]:
    ids: set[int] = set()
    found: list[RawAttribute] = []
    for fact in (*tuple(exact), *tuple(related)):
        for contributor in fact.contributors:
            if id(contributor) not in ids:
                ids.add(id(contributor))
                found.append(contributor)
    for item in ambiguous:
        candidates = getattr(item, "candidates", ())
        attribute = getattr(item, "raw_attribute", None)
        if requested in candidates and attribute is not None and id(attribute) not in ids:
            ids.add(id(attribute))
            found.append(attribute)
    return tuple(found)


def _field_status(results: Iterable[TargetedQueryResult]) -> FieldSearchStatus:
    items = tuple(results)
    if any(item.useful_evidence_found for item in items):
        return "success"
    statuses = {item.discovery_status for item in items}
    fetch_statuses = {
        source["status"] for item in items for source in item.fetched_sources
    }
    if "blocked" in statuses or "blocked" in fetch_statuses:
        return "blocked"
    if "error" in statuses or "error" in fetch_statuses:
        return "error"
    return "unresolved"


def _run_targeted_search(
    plan: TargetedSearchPlan,
    analysis: GapAnalysisResult,
    identity: ProductIdentity,
    discovery: DiscoveryRunner,
    fetcher: Fetcher,
    extractor: Extractor,
) -> TargetedSearchResult:
    gap_index = analysis.by_name
    fetch_cache: dict[str, FetchResult] = {}
    extraction_cache: dict[str, tuple[list[RawAttribute], object]] = {}
    field_results: list[TargetedFieldResult] = []

    for canonical_name, queries in plan.by_field.items():
        gap = gap_index[canonical_name]
        query_results: list[TargetedQueryResult] = []
        inspected = 0
        stop_reason: StopReason = "query_limit"
        stop_field = False

        for targeted_query in queries[:plan.config.max_queries_per_field]:
            outcome = discovery(identity, targeted_query.query)
            fetched: list[FetchResult] = []
            exact_facts: list[CanonicalAttribute] = []
            related_facts: list[CanonicalAttribute] = []
            relevant_raw: list[RawAttribute] = []
            rejected: list[CandidateRejection] = []
            strong_official = False

            for candidate in outcome.candidates[:plan.config.max_candidates_per_query]:
                if inspected >= plan.config.max_candidates_per_field:
                    stop_reason = "candidate_limit"
                    stop_field = True
                    break
                inspected += 1
                rejection = _candidate_identity_rejection(candidate, gap, identity)
                if rejection:
                    rejected.append(CandidateRejection(candidate, rejection))
                    continue
                url = canonicalize_url(str(candidate.get("url") or ""))
                if not url:
                    rejected.append(CandidateRejection(candidate, "Candidate URL is invalid."))
                    continue
                if url not in fetch_cache:
                    fetch_cache[url] = fetcher(candidate)
                source = dict(fetch_cache[url])
                source["discovery_metadata"] = dict(candidate)
                for key in (
                    "source_type", "authority_status", "authority_evidence_url",
                    "model_relevance", "identity_relation",
                ):
                    source[key] = candidate.get(key)
                fetched.append(source)  # type: ignore[arg-type]
                if source["status"] != "success":
                    continue
                if url not in extraction_cache:
                    raw = extractor(source)
                    extraction_cache[url] = (
                        raw,
                        map_attributes(raw, category=plan.category),
                    )
                raw, mapped = extraction_cache[url]
                canonical = list(mapped.canonical_attributes)
                exact = [fact for fact in canonical if fact.canonical_name == canonical_name]
                related_names = _related_names(canonical_name) - {canonical_name}
                related = [fact for fact in canonical if fact.canonical_name in related_names]
                exact_facts.extend(exact)
                related_facts.extend(related)
                relevant_raw.extend(
                    _relevant_raw(canonical_name, raw, exact, related, mapped.ambiguous)
                )
                if exact and (
                    candidate.get("authority_status") == "verified"
                    and candidate.get("source_type") in {"manufacturer", "official_document"}
                ):
                    strong_official = True

            useful = bool(exact_facts)
            query_results.append(TargetedQueryResult(
                requested_canonical_field=canonical_name,
                query=targeted_query.query,
                query_intent=targeted_query.search_intent,
                discovery_candidates=tuple(outcome.candidates),
                fetched_sources=tuple(fetched),
                extracted_relevant_facts=tuple(dict.fromkeys(relevant_raw)),
                mapped_candidate_facts=tuple(exact_facts),
                related_candidate_facts=tuple(related_facts),
                rejected_candidates=tuple(rejected),
                discovery_status=outcome.search_status,
                issues=tuple(outcome.issues),
                useful_evidence_found=useful,
            ))
            if outcome.search_status == "blocked":
                stop_reason = "discovery_blocked"
                break
            if outcome.search_status == "error":
                stop_reason = "discovery_error"
                break
            if strong_official and plan.config.stop_on_strong_official_evidence:
                stop_reason = "strong_official_evidence"
                break
            if stop_field:
                break

        if not queries:
            stop_reason = "no_queries"
        status = _field_status(query_results)
        field_results.append(TargetedFieldResult(
            gap=gap,
            query_results=tuple(query_results),
            search_status=status,
            useful_evidence_found=status == "success",
            stop_reason=stop_reason,
        ))

    return TargetedSearchResult(
        plan=plan,
        fields=field_results,
        fetched_urls=tuple(fetch_cache),
        fetch_count=len(fetch_cache),
    )


def run_targeted_search(
    plan: TargetedSearchPlan,
    analysis: GapAnalysisResult,
    identity: ProductIdentity,
    *,
    market: str = "global",
    searcher: Searcher | None = None,
    discovery: DiscoveryRunner | None = None,
    fetcher: Fetcher = fetch_candidate,
    extractor: Extractor = extract_attributes,
) -> TargetedSearchResult:
    """Execute a plan with per-run URL caching; returned facts stay provisional."""
    if plan.category != analysis.category:
        raise ValueError("plan and gap analysis categories differ")
    if discovery is not None:
        return _run_targeted_search(plan, analysis, identity, discovery, fetcher, extractor)
    if searcher is not None:
        runner = lambda item, query: discover_identity_query_with_status(
            item, query, market, searcher,
        )
        return _run_targeted_search(plan, analysis, identity, runner, fetcher, extractor)
    with GoogleSearchSession(market) as session:
        runner = lambda item, query: discover_identity_query_with_status(
            item, query, market, session.search,
        )
        return _run_targeted_search(plan, analysis, identity, runner, fetcher, extractor)
