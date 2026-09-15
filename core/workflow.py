"""Application-level orchestration for the completed Stage 1-7 pipeline.

This module only coordinates existing stage APIs. Search and transport
boundaries are injectable so tests and application adapters do not need live
network access.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Mapping, cast

from core.category import CategoryResult, detect_category
from core.discovery import (
    SUPPORTED_MARKETS,
    Candidate,
    DiscoveryOutcome,
    ProviderAttempt,
    ResilientSearchSession,
    canonicalize_url,
    discover_identity_query_with_status,
    discover_identity_with_status,
)
from core.extract import RawAttribute, extract_attributes
from core.fetch import FetchResult, fetch_candidate
from core.gaps import GapAnalysisResult, analyze_gaps
from core.identity import IdentityEvidence, ProductIdentity, resolve_product_identity
from core.mapping import MappingResult, map_attributes
from core.profile import FinalProductProfile, build_final_profile
from core.quality import QualityAssessment, assess_product_quality
from core.schema import (
    AttributeDefinition,
    Priority,
    SchemaDiagnostics,
    analyze_schema_coverage,
    extend_schema_with_discovered,
)
from core.targeted_search import (
    TargetedSearchConfig,
    TargetedSearchPlan,
    TargetedSearchResult,
    build_targeted_search_plan,
    run_targeted_search,
)
from core.validation import ValidatedProductProfile, validate_product_profile


WORKFLOW_VERSION = "1.0"

InitialDiscovery = Callable[[ProductIdentity, str], DiscoveryOutcome]
TargetedDiscovery = Callable[[ProductIdentity, str, str], DiscoveryOutcome]
Fetcher = Callable[[Mapping[str, object]], FetchResult]
Extractor = Callable[[dict[str, object]], list[RawAttribute]]


def _provider_attempt_data(attempt: ProviderAttempt) -> dict[str, object]:
    return {
        "provider": attempt.provider,
        "query": attempt.query,
        "status": attempt.status,
        "result_count": attempt.result_count,
        "message": attempt.message,
        "is_fallback": attempt.is_fallback,
    }


def _initial_discovery(identity: ProductIdentity, market: str) -> DiscoveryOutcome:
    return discover_identity_with_status(identity, market)


def _targeted_discovery(
    identity: ProductIdentity,
    query: str,
    market: str,
) -> DiscoveryOutcome:
    return discover_identity_query_with_status(identity, query, market)


@dataclass(frozen=True, slots=True)
class WorkflowServices:
    """Network-facing boundaries used by the workflow."""

    discover_initial: InitialDiscovery = _initial_discovery
    discover_targeted: TargetedDiscovery = _targeted_discovery
    fetch: Fetcher = fetch_candidate
    extract: Extractor = extract_attributes


@dataclass(frozen=True, slots=True)
class ProductWorkflowRequest:
    raw_name: str
    brand: str | None = None
    identity_evidence: tuple[IdentityEvidence | Mapping[str, str], ...] = ()
    market: str = "global"
    max_initial_sources: int = 5
    minimum_search_priority: Priority = "medium"
    targeted_search_enabled: bool = True
    targeted_config: TargetedSearchConfig = field(default_factory=TargetedSearchConfig)

    def __post_init__(self) -> None:
        raw_name = " ".join((self.raw_name or "").split())
        brand = " ".join((self.brand or "").split()) or None
        if not raw_name:
            raise ValueError("raw_name is required")
        if self.market not in SUPPORTED_MARKETS:
            raise ValueError(f"Unsupported market: {self.market}")
        if self.max_initial_sources < 1:
            raise ValueError("max_initial_sources must be positive")
        if self.minimum_search_priority not in {"critical", "high", "medium", "low"}:
            raise ValueError(
                f"unsupported minimum_search_priority: {self.minimum_search_priority}"
            )
        object.__setattr__(self, "raw_name", raw_name)
        object.__setattr__(self, "brand", brand)
        object.__setattr__(self, "identity_evidence", tuple(self.identity_evidence))

    @classmethod
    def from_parts(
        cls,
        brand: str,
        model: str,
        article: str | None = None,
        **options: object,
    ) -> "ProductWorkflowRequest":
        """Build a request while treating a named article as explicit evidence."""
        cleaned_brand = " ".join((brand or "").split())
        cleaned_model = " ".join((model or "").split())
        if not cleaned_brand or not cleaned_model:
            raise ValueError("brand and model are required")
        evidence = list(options.pop("identity_evidence", ()))
        if article and article.strip():
            evidence.append(IdentityEvidence(
                "manufacturer_article",
                article,
                "application input",
            ))
        return cls(
            raw_name=f"{cleaned_brand} {cleaned_model}",
            brand=cleaned_brand,
            identity_evidence=tuple(evidence),
            **options,
        )


@dataclass(frozen=True, slots=True)
class ProductWorkflowResult:
    request: ProductWorkflowRequest
    identity: ProductIdentity
    discovery: DiscoveryOutcome
    selected_candidates: tuple[Candidate, ...]
    fetched_sources: tuple[FetchResult, ...]
    raw_attributes: tuple[RawAttribute, ...]
    category: CategoryResult
    schema: tuple[AttributeDefinition, ...]
    mapping: MappingResult
    coverage: SchemaDiagnostics
    gaps: GapAnalysisResult
    targeted_plan: TargetedSearchPlan
    targeted_search: TargetedSearchResult | None
    validated_profile: ValidatedProductProfile
    final_profile: FinalProductProfile
    quality: QualityAssessment

    @property
    def fetch_failures(self) -> tuple[FetchResult, ...]:
        return tuple(
            source for source in self.fetched_sources
            if source.get("status") != "success"
        )

    @property
    def summary(self) -> dict[str, object]:
        return {
            "workflow_version": WORKFLOW_VERSION,
            "discovery_status": self.discovery.search_status,
            "candidate_count": len(self.discovery.candidates),
            "provider_attempts": [
                _provider_attempt_data(item) for item in self.discovery.provider_attempts
            ],
            "selected_candidate_count": len(self.selected_candidates),
            "initial_fetch_count": len(self.fetched_sources),
            "successful_initial_fetch_count": sum(
                source.get("status") == "success" for source in self.fetched_sources
            ),
            "raw_attribute_count": len(self.raw_attributes),
            "targeted_query_count": len(self.targeted_plan.queries),
            "targeted_fetch_count": (
                self.targeted_search.fetch_count if self.targeted_search else 0
            ),
        }


def _source_metadata(
    fetched_sources: tuple[FetchResult, ...],
) -> dict[str, Mapping[str, object]]:
    metadata: dict[str, Mapping[str, object]] = {}
    for source in fetched_sources:
        combined: dict[str, object] = dict(source)
        combined.update(source.get("discovery_metadata") or {})
        for key in ("source_url", "final_url"):
            url = str(source.get(key) or "").strip()
            if url:
                metadata.setdefault(url, combined)
    return metadata


def select_source_candidates(
    candidates: list[Candidate],
    limit: int,
) -> tuple[Candidate, ...]:
    """Apply canonical dedupe and final score ordering before the fetch limit."""
    if limit < 1:
        raise ValueError("source limit must be positive")
    deduplicated: dict[str, Candidate] = {}
    for candidate in candidates:
        if candidate.get("relevance_relation") == "reject":
            continue
        if (
            candidate.get("identity_relation") == "different_model"
            or candidate.get("model_relevance") == "different_model"
            or candidate.get("model_match") == "mismatch"
        ):
            continue
        url = canonicalize_url(candidate["url"])
        if not url:
            continue
        previous = deduplicated.get(url)
        if previous is None or candidate["score"] > previous["score"]:
            deduplicated[url] = candidate
    ranked = sorted(
        deduplicated.values(),
        key=lambda item: (-item["score"], item["url"]),
    )
    return tuple(ranked[:limit])


def _product_texts(fetched_sources: tuple[FetchResult, ...]) -> list[str]:
    return [
        str(source.get("discovery_metadata", {}).get("title") or "")
        for source in fetched_sources
        if source.get("identity_relation") != "different_model"
    ]


def _candidate_fetch_view(
    source: FetchResult,
    candidate: Mapping[str, object],
) -> FetchResult:
    """Attach current discovery provenance to a reusable transport result."""
    result = dict(source)
    result["discovery_metadata"] = dict(candidate)
    for field_name in (
        "source_type",
        "authority_status",
        "authority_evidence_url",
        "model_relevance",
        "identity_relation",
    ):
        value = candidate.get(field_name)
        result[field_name] = str(value) if value is not None else None
    return cast(FetchResult, result)


def _run_product_workflow_with_services(
    request: ProductWorkflowRequest,
    active: WorkflowServices,
) -> ProductWorkflowResult:
    """Run Stages 1-7 once and retain every stage result for inspection."""
    identity = resolve_product_identity(
        request.raw_name,
        brand=request.brand,
        evidence=request.identity_evidence,
    )
    discovery = active.discover_initial(identity, request.market)
    selected_candidates = select_source_candidates(
        discovery.candidates,
        request.max_initial_sources,
    )

    fetch_cache: dict[str, FetchResult] = {}

    def fetch_once(candidate: Mapping[str, object]) -> FetchResult:
        url = str(candidate.get("url") or "")
        key = canonicalize_url(url) or url
        if key not in fetch_cache:
            fetch_cache[key] = active.fetch(candidate)
        return _candidate_fetch_view(fetch_cache[key], candidate)

    fetched_sources = tuple(
        fetch_once(candidate)
        for candidate in selected_candidates
    )
    raw_attributes = tuple(
        attribute
        for source in fetched_sources
        if source.get("status") == "success"
        for attribute in active.extract(source)
    )
    category = detect_category(
        identity,
        raw_attributes,
        product_texts=_product_texts(fetched_sources),
    )
    schema = tuple(extend_schema_with_discovered(category, raw_attributes))
    mapping = map_attributes(raw_attributes, schema=schema, category=category)
    coverage = analyze_schema_coverage(category, mapping, identity=identity)
    gaps = analyze_gaps(
        category,
        mapping,
        identity=identity,
        coverage=coverage,
        schema=schema,
        minimum_search_priority=request.minimum_search_priority,
    )
    targeted_plan = build_targeted_search_plan(
        gaps,
        identity,
        config=request.targeted_config,
    )
    targeted_search: TargetedSearchResult | None = None
    if request.targeted_search_enabled:
        targeted_search = run_targeted_search(
            targeted_plan,
            gaps,
            identity,
            market=request.market,
            discovery=lambda item, query: active.discover_targeted(
                item, query, request.market,
            ),
            fetcher=fetch_once,
            extractor=active.extract,
        )

    validated = validate_product_profile(
        identity,
        category,
        mapping,
        targeted_search=targeted_search,
        gap_analysis=gaps,
        source_metadata=_source_metadata(fetched_sources),
        schema=schema,
    )
    profile = build_final_profile(
        validated,
        category_result=category,
        schema=schema,
        metadata={
            "workflow_version": WORKFLOW_VERSION,
            "discovery_status": discovery.search_status,
            "initial_candidate_count": len(discovery.candidates),
            "initial_candidate_count_before_relevance_gate": (
                len(discovery.candidates) + len(discovery.rejected_candidates)
            ),
            "initial_rejected_candidate_count": len(discovery.rejected_candidates),
            "initial_provider_attempts": [
                _provider_attempt_data(item) for item in discovery.provider_attempts
            ],
            "selected_candidate_count": len(selected_candidates),
            "initial_fetch_count": len(fetched_sources),
            "targeted_search_enabled": request.targeted_search_enabled,
            "targeted_query_count": len(targeted_plan.queries),
            "targeted_fetch_count": targeted_search.fetch_count if targeted_search else 0,
        },
    )
    return ProductWorkflowResult(
        request=request,
        identity=identity,
        discovery=discovery,
        selected_candidates=selected_candidates,
        fetched_sources=fetched_sources,
        raw_attributes=raw_attributes,
        category=category,
        schema=schema,
        mapping=mapping,
        coverage=coverage,
        gaps=gaps,
        targeted_plan=targeted_plan,
        targeted_search=targeted_search,
        validated_profile=validated,
        final_profile=profile,
        quality=assess_product_quality(profile),
    )


def _discover_initial_with_released_browser(
    search: ResilientSearchSession,
    identity: ProductIdentity,
    market: str,
) -> DiscoveryOutcome:
    """Finish the discovery browser lifecycle before document fetch begins."""
    try:
        return discover_identity_with_status(
            identity,
            market,
            searcher=search.search_with_status,
        )
    finally:
        search.release_transient_resources()


def _discover_targeted_with_released_browser(
    search: ResilientSearchSession,
    identity: ProductIdentity,
    query: str,
    market: str,
) -> DiscoveryOutcome:
    """Release a targeted discovery browser before its candidate is fetched."""
    try:
        return discover_identity_query_with_status(
            identity,
            query,
            market,
            searcher=search.search_with_status,
        )
    finally:
        search.release_transient_resources()


def run_product_workflow(
    request: ProductWorkflowRequest,
    *,
    services: WorkflowServices | None = None,
) -> ProductWorkflowResult:
    """Run the workflow with one resilient provider session per live request."""
    if services is not None:
        return _run_product_workflow_with_services(request, services)

    with ResilientSearchSession(request.market) as search:
        live_services = WorkflowServices(
            discover_initial=lambda identity, market: _discover_initial_with_released_browser(
                search, identity, market,
            ),
            discover_targeted=lambda identity, query, market: (
                _discover_targeted_with_released_browser(
                    search, identity, query, market,
                )
            ),
        )
        return _run_product_workflow_with_services(request, live_services)


# Short application-facing alias.
run_workflow = run_product_workflow
