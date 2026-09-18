"""Application-level orchestration for the completed Stage 1-7 pipeline.

This module only coordinates existing stage APIs. Search and transport
boundaries are injectable so tests and application adapters do not need live
network access.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import time
from typing import Callable, Mapping, cast
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from core.authority import ELEVATED_ROLES, TrustedSource, resolve_authority
from core.budget import BudgetExhaustedError, WallClockBudget
from core.category import CategoryResult, detect_category
from core.discovery import (
    SUPPORTED_MARKETS,
    Candidate,
    DEFAULT_PROVIDER_TIMEOUTS,
    DiscoveryRuntimeConfig,
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
from core.provider_health import ProviderHealthStore
from core.identity import (
    IdentityEvidence,
    ProductIdentity,
    base_model_in_text,
    resolve_product_identity,
)
from core.mapping import MappingResult, map_attributes
from core.attribute_catalog import extension_definitions
from core.official_spec_table import extract_official_section_specs
from core.official_source import (
    OFFICIAL_SOURCE_TYPES,
    REDIRECTED_AWAY_REASON,
    build_official_resolution,
    extract_auxiliary_links,
    extract_official_spec_links,
    select_product_image_records,
    PROSE_FACT_CONTEXT,
    extract_official_prose_facts,
    is_official,
    redirected_away_from_exact_page,
    source_priority,
)
from core.profile import FinalProductProfile, build_final_profile
from core.quality import QualityAssessment, assess_product_quality
from core.schema import (
    AttributeDefinition,
    Priority,
    SchemaDiagnostics,
    analyze_schema_coverage,
    extend_schema_with_discovered,
    get_attribute_schema,
    resolve_attribute_definition,
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
        "duration_seconds": attempt.duration_seconds,
        "timeout_seconds": attempt.timeout_seconds,
        "timed_out": attempt.timed_out,
        "blocked": attempt.blocked,
        "parse_failure": attempt.parse_failure,
        "exception_class": attempt.exception_class,
        "circuit_open": attempt.circuit_open,
        "budget_exhausted": attempt.budget_exhausted,
        "raw_result_count": attempt.raw_result_count,
        "parsed_result_count": attempt.parsed_result_count,
        "deduped_result_count": attempt.deduped_result_count,
        "transport": attempt.transport,
        "failure_class": attempt.failure_class,
        "shared_circuit_open": attempt.shared_circuit_open,
        "retried": attempt.retried,
        "provider_time_capped": attempt.provider_time_capped,
        "effective_query": attempt.effective_query,
        "exact_model_hit": attempt.exact_model_hit,
        "official_domain_hit": attempt.official_domain_hit,
        "budget_before_seconds": attempt.budget_before_seconds,
        "budget_after_seconds": attempt.budget_after_seconds,
        "independent_success_without_ddg": attempt.independent_success_without_ddg,
        "accepted_candidate_count": attempt.accepted_candidate_count,
        "rejected_candidate_count": attempt.rejected_candidate_count,
        "official_discovery_method": attempt.discovery_method,
        "official_method_requests": dict(attempt.method_requests),
        "official_candidate_count": attempt.candidate_count,
        "official_exact_model_candidate_count": attempt.exact_model_candidate_count,
        "accepted_official_url": attempt.accepted_official_url,
        "official_failure_reason": attempt.failure_reason,
        "browser_invoked": attempt.browser_invoked,
        "browser_reason": attempt.browser_reason,
        "browser_pages_opened": attempt.browser_pages_opened,
        "browser_navigation_seconds": attempt.browser_navigation_seconds,
        "browser_rendered_candidate_count": attempt.browser_rendered_candidate_count,
        "browser_xhr_candidate_count": attempt.browser_xhr_candidate_count,
        "browser_captcha_detected": attempt.browser_captcha_detected,
        "browser_budget_used_seconds": attempt.browser_budget_used_seconds,
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
    clock: Callable[[], float] = time.monotonic


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
    wall_clock_budget_seconds: float = 90.0
    provider_timeouts: Mapping[str, float] = field(
        default_factory=lambda: dict(DEFAULT_PROVIDER_TIMEOUTS),
    )

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
        if self.wall_clock_budget_seconds <= 0:
            raise ValueError("wall_clock_budget_seconds must be positive")
        runtime_config = DiscoveryRuntimeConfig(self.provider_timeouts)
        object.__setattr__(self, "raw_name", raw_name)
        object.__setattr__(self, "brand", brand)
        object.__setattr__(self, "identity_evidence", tuple(self.identity_evidence))
        object.__setattr__(self, "wall_clock_budget_seconds", float(self.wall_clock_budget_seconds))
        object.__setattr__(self, "provider_timeouts", dict(runtime_config.provider_timeouts))

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
    """Apply canonical dedupe and final score ordering before the fetch limit.

    Preserve one verified exact official document when ranking would otherwise
    fill every slot with broader first-party pages. This is a source-role
    diversity rule, not a product override; relevance and exact-model gates
    have already run.
    """
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
    has_exact_official = any(
        item.get("authority_status") == "verified"
        and item.get("source_type") in {"manufacturer", "official_document"}
        and item.get("model_match") == "exact"
        and item.get("identity_relation") in {"same_base_model", "exact_variant"}
        for item in deduplicated.values()
    )

    def priority(item: Candidate) -> tuple[int, int, str]:
        verified = item.get("authority_status") == "verified"
        exact = (
            item.get("model_match") == "exact"
            and item.get("identity_relation") in {"same_base_model", "exact_variant"}
        )
        source_type = item.get("source_type")
        verification_fetch = (
            verified
            and source_type in {"manufacturer", "official_document"}
            and item.get("relevance_relation") == "weak"
            and item.get("identity_relation") == "unknown"
            and any(
                "content verification" in str(reason).casefold()
                for reason in item.get("relevance_reasons") or ()
            )
        )
        ordinary_weak = (
            item.get("relevance_relation") == "weak" and not verification_fetch
        )
        if ordinary_weak:
            tier = 6
        elif verified and exact and source_type == "official_document":
            tier = 0
        elif verified and exact and source_type == "manufacturer":
            tier = 1
        elif source_type == "specialized_reference" and exact:
            tier = 2 if has_exact_official else 3
        elif verification_fetch:
            tier = 3 if has_exact_official else 2
        elif item.get("relevance_relation") != "weak":
            tier = 4
        else:
            tier = 5
        return tier, -int(item["score"]), item["url"]

    ranked = sorted(deduplicated.values(), key=priority)
    # First pass preserves transport/source diversity. Repeated regional
    # mirrors from one host and role are poor uses of a small fetch budget,
    # especially when the host blocks automated access consistently.
    selected: list[Candidate] = []
    seen_families: set[tuple[str, str]] = set()
    weak_selected = False
    for candidate in ranked:
        verification_fetch = any(
            "content verification" in str(reason).casefold()
            for reason in candidate.get("relevance_reasons") or ()
        )
        ordinary_weak = (
            candidate.get("relevance_relation") == "weak" and not verification_fetch
        )
        if ordinary_weak and weak_selected:
            continue
        family = (
            (urlparse(candidate["url"]).hostname or "").lower().removeprefix("www."),
            str(candidate.get("source_type") or "other"),
        )
        if family in seen_families:
            continue
        selected.append(candidate)
        weak_selected = weak_selected or ordinary_weak
        seen_families.add(family)
        if len(selected) == limit:
            return tuple(selected)
    # If diversity cannot fill the configured capacity, retain deterministic
    # score order for the remaining mirrors.
    for candidate in ranked:
        if candidate in selected:
            continue
        verification_fetch = any(
            "content verification" in str(reason).casefold()
            for reason in candidate.get("relevance_reasons") or ()
        )
        ordinary_weak = (
            candidate.get("relevance_relation") == "weak" and not verification_fetch
        )
        if ordinary_weak and weak_selected:
            continue
        selected.append(candidate)
        weak_selected = weak_selected or ordinary_weak
        if len(selected) == limit:
            break
    return tuple(selected)


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


def _verify_fetched_identity(
    source: FetchResult,
    candidate: Mapping[str, object],
    identity: ProductIdentity,
) -> FetchResult:
    """Upgrade only a verification-fetch whose body proves the exact model.

    Search query text is never evidence. The transition from unknown to
    same-base-model is based on successfully fetched page content, while all
    pre-fetch different-model decisions remain irreversible.
    """
    if source.get("status") != "success":
        return source
    if candidate.get("identity_relation") != "unknown":
        return source
    if candidate.get("authority_status") != "verified":
        return source
    if candidate.get("source_type") not in {"manufacturer", "official_document"}:
        return source
    if not any(
        "content verification" in str(reason).casefold()
        for reason in candidate.get("relevance_reasons") or ()
    ):
        return source
    model = identity.base_model or identity.commercial_model
    html = str(source.get("html") or "")
    soup = BeautifulSoup(html, "html.parser")
    identity_texts = [
        str(source.get("final_url") or ""),
        soup.title.get_text(" ", strip=True) if soup.title else "",
    ]
    identity_texts.extend(
        heading.get_text(" ", strip=True)
        for heading in soup.find_all("h1", limit=3)
    )
    for selector in ('meta[property="og:title"]', 'meta[name="twitter:title"]'):
        tag = soup.select_one(selector)
        if tag and tag.get("content"):
            identity_texts.append(str(tag.get("content")))
    if not model or not base_model_in_text(model, " ".join(identity_texts)):
        return source
    source["model_relevance"] = "exact_base_model"
    source["identity_relation"] = "same_base_model"
    metadata = dict(source.get("discovery_metadata") or candidate)
    metadata["model_relevance"] = "exact_base_model"
    metadata["identity_relation"] = "same_base_model"
    metadata["content_identity_verified"] = True
    source["discovery_metadata"] = metadata
    return source


def _reject_redirected_official_identity(
    source: FetchResult,
    candidate: Mapping[str, object],
    identity: ProductIdentity,
) -> FetchResult:
    """Withdraw exact-model identity from an official URL that redirected away.

    An official product URL that bounces to a category/landing page proves
    nothing about the model (and its page lists many products), so its facts
    must not be accepted as exact-model official evidence. The decision is
    recorded for the Official Source Resolution Gate.
    """
    if not redirected_away_from_exact_page(source, candidate, identity):
        return source
    source["official_rejection_reason"] = REDIRECTED_AWAY_REASON
    source["model_relevance"] = "unknown"
    source["identity_relation"] = "unknown"
    metadata = dict(source.get("discovery_metadata") or candidate)
    metadata["model_relevance"] = "unknown"
    metadata["identity_relation"] = "unknown"
    metadata["official_rejection_reason"] = REDIRECTED_AWAY_REASON
    source["discovery_metadata"] = metadata
    return source


def _fetch_domain(source: Mapping[str, object]) -> str:
    url = str(source.get("final_url") or source.get("source_url") or "")
    return (urlparse(url).hostname or "").lower().removeprefix("www.")


def _role_source_type(role: str) -> str:
    """Map a fine-grained authority role onto the coarse, pre-existing
    source_type vocabulary that core.validation._authority_rank already
    understands, so validation ranking/thresholds are never touched.

    Only "manufacturer" reaches the rank-4 (Confirmed-eligible) tier;
    official_distributor/authorized_dealer share the existing "distributor"
    rank-2 tier with plain retailers - corroboration proves a real
    relationship, but it does not by itself make a fact reliable enough to
    confirm alone, exactly as retailer/marketplace evidence already cannot.
    """
    if role == "manufacturer":
        return "manufacturer"
    if role in ("official_distributor", "authorized_dealer"):
        return "distributor"
    return role


def _resolve_authority_roles(sources: list[FetchResult], brand: str) -> None:
    """Post-pass: recover a generic, evidence-based authority role for every
    successfully fetched HTML page still "unknown" after discovery, using
    the full set of pages fetched in this run (see core.authority).

    Mutates each FetchResult dict in place (FetchResult is a plain dict;
    TypedDict is a type-checking construct only), so it also updates the
    dicts nested inside an already-built TargetedSearchResult without
    reconstructing its frozen dataclasses. Runs once after all fetching
    (initial and targeted) is complete so corroboration sees the full,
    order-independent set of pages fetched in this run - never a single
    self-declared page in isolation.
    """
    if not brand:
        return
    trusted_sources = tuple(
        TrustedSource(domain=_fetch_domain(source), html=str(source.get("html") or ""))
        for source in sources
        if source.get("status") == "success"
        and source.get("document_type") == "html"
        and source.get("authority_status") == "verified"
        and source.get("source_type") == "manufacturer"
        and _fetch_domain(source)
    )
    for source in sources:
        if source.get("status") != "success" or source.get("document_type") != "html":
            continue
        if source.get("authority_status") not in (None, "unknown"):
            continue
        domain = _fetch_domain(source)
        if not domain:
            continue
        assessment = resolve_authority(
            str(source.get("html") or ""), str(source.get("text") or ""),
            domain, brand, trusted_sources=trusted_sources,
        )
        source["authority_role"] = assessment.role
        if assessment.role not in ELEVATED_ROLES:
            continue
        evidence_url = str(source.get("final_url") or source.get("source_url") or "")
        source["authority_status"] = "verified"
        source["source_type"] = _role_source_type(assessment.role)
        source["authority_evidence_url"] = evidence_url
        metadata = dict(source.get("discovery_metadata") or {})
        metadata["authority_status"] = source["authority_status"]
        metadata["source_type"] = source["source_type"]
        metadata["authority_evidence_url"] = evidence_url
        metadata["authority_reason"] = assessment.reason
        source["discovery_metadata"] = metadata


def _official_identity_attributes(
    source: FetchResult,
    identity: ProductIdentity,
    extracted: list[RawAttribute],
) -> list[RawAttribute]:
    """Materialize identity only when an exact verified first-party page says it.

    The values come from the resolved request identity, but the evidence gate is
    the fetched official document itself. This fills a common structured-data
    omission without treating the user's input or a search snippet as proof.
    """
    if source.get("status") != "success":
        return []
    if source.get("authority_status") != "verified":
        return []
    if source.get("source_type") not in {"manufacturer", "official_document"}:
        return []
    if source.get("model_relevance") != "exact_base_model":
        return []
    if source.get("identity_relation") not in {"same_base_model", "exact_variant"}:
        return []

    model = identity.base_model or identity.commercial_model
    page_text = " ".join((str(source.get("text") or ""), str(source.get("html") or "")))
    if not model or not base_model_in_text(model, page_text):
        return []

    present = {
        definition.canonical_name
        for item in extracted
        if (definition := resolve_attribute_definition(item.name, "unknown")) is not None
    }
    source_url = str(source.get("final_url") or source.get("source_url") or "")
    source_type = str(source.get("source_type") or "manufacturer")
    values = (("Brand", identity.brand), ("Model", model))
    return [
        RawAttribute(
            name=name,
            value=value,
            unit=None,
            source_url=source_url,
            source_type=source_type,
            evidence=(
                f"Verified first-party page contains the exact model phrase {model!r}."
            ),
            extraction_method="structured_data",
            confidence="high",
            raw_value=value,
            attribute_kind="identity",
            context="verified official exact-model page",
        )
        for name, value in values
        if value and name.casefold() not in present
    ]


def _present_canonical_names(attributes: list[RawAttribute]) -> set[str]:
    return {
        definition.canonical_name
        for item in attributes
        if (definition := resolve_attribute_definition(item.name, "unknown")) is not None
    }


def _all_fetched_sources(
    fetched: tuple[FetchResult, ...],
    targeted_search: TargetedSearchResult | None,
) -> list[FetchResult]:
    found = list(fetched)
    seen = {str(item.get("source_url")) for item in found}
    for field_result in (targeted_search.fields if targeted_search else ()):
        for query_result in field_result.query_results:
            for source in query_result.fetched_sources:
                if str(source.get("source_url")) not in seen:
                    seen.add(str(source.get("source_url")))
                    found.append(source)
    return found


def _official_priority_metadata(
    identity: ProductIdentity,
    discovery: DiscoveryOutcome,
    selected: tuple[Candidate, ...],
    fetched: tuple[FetchResult, ...],
    targeted_search: TargetedSearchResult | None,
    raw_attributes: tuple[RawAttribute, ...],
    mapping: MappingResult,
    schema: tuple[AttributeDefinition, ...],
    validated: ValidatedProductProfile,
) -> dict[str, object]:
    """Stage 31.4 gate record, official images, auxiliary links, source order."""
    all_sources = _all_fetched_sources(fetched, targeted_search)
    image_records = select_product_image_records(
        all_sources, identity.base_model or identity.commercial_model,
    )
    auxiliary: list[dict[str, str]] = []
    for source in all_sources:
        if is_official(source):
            continue
        for link in extract_auxiliary_links(source):
            if not any(item["url"] == link["url"] for item in auxiliary):
                auxiliary.append(link)
    ordered = source_priority(
        (
            str(item.get("final_url") or item.get("source_url") or ""),
            item.get("source_type"),
            item.get("authority_status"),
        )
        for item in all_sources
        if item.get("status") == "success"
        and item.get("identity_relation") != "different_model"
        and not item.get("official_rejection_reason")
    )
    resolution = build_official_resolution(
        identity=identity,
        candidates=discovery.candidates,
        rejected_candidates=discovery.rejected_candidates,
        selected=selected,
        fetched_sources=all_sources,
        raw_attributes=raw_attributes,
        canonical_attributes=mapping.canonical_attributes,
        schema_names=[item.canonical_name for item in schema if item.scope != "discovered"],
        provider_attempts=discovery.provider_attempts,
    )
    # What the user-facing profile finally rests on, so an "accessible official
    # page" that contributed nothing is visible rather than implied.
    schema_fields = {item.canonical_name for item in schema if item.scope != "discovered"}
    confirmed = [
        fact for fact in validated.facts
        if fact.status == "Confirmed" and fact.canonical_name in schema_fields
    ]
    resolution["official_final_profile_attribute_count"] = sum(
        fact.authority_status == "verified"
        and fact.supporting_facts[0].effective_source_type in OFFICIAL_SOURCE_TYPES
        for fact in confirmed
    )
    resolution["secondary_final_profile_attribute_count"] = (
        len(confirmed) - resolution["official_final_profile_attribute_count"]
    )
    return {
        "official_source_resolution": resolution,
        "product_images": [item["url"] for item in image_records],
        "product_image_records": image_records,
        "auxiliary_links": auxiliary,
        "source_priority": ordered,
    }


def _schema_with_official_extensions(
    category: CategoryResult,
    raw_attributes: tuple[RawAttribute, ...],
) -> tuple[AttributeDefinition, ...]:
    """Category schema + dynamic canonical attributes from official atomic facts.

    The schema says what a category is *expected* to have; it never limits what
    an official source may contribute. A hinted canonical attribute the schema
    lacks becomes a (non-expected, user-facing) definition for this run; it
    replaces the "discovered" placeholder the generic path would have made.
    """
    definitions = {
        item.canonical_name: item
        for item in extend_schema_with_discovered(category, raw_attributes)
    }
    static = {item.canonical_name for item in get_attribute_schema(category)}
    hints = [
        (item.canonical, item.name, item.value.casefold() in {"yes", "no"})
        for item in raw_attributes if item.canonical
    ]
    for definition in extension_definitions(hints, static):
        definitions[definition.canonical_name] = definition
    return tuple(definitions.values())


def _run_product_workflow_with_services(
    request: ProductWorkflowRequest,
    active: WorkflowServices,
    *,
    budget: WallClockBudget | None = None,
) -> ProductWorkflowResult:
    """Run Stages 1-7 once and retain every stage result for inspection."""
    budget = budget or WallClockBudget(request.wall_clock_budget_seconds, active.clock)
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
        view = _candidate_fetch_view(fetch_cache[key], candidate)
        view = _verify_fetched_identity(view, candidate, identity)
        return _reject_redirected_official_identity(view, candidate, identity)

    fetched: list[FetchResult] = []
    extracted: list[RawAttribute] = []
    spec_table_diagnostics: list[dict[str, object]] = []
    queue: list[Mapping[str, object]] = list(selected_candidates)
    queued = {canonicalize_url(str(item.get("url") or "")) for item in queue}
    for candidate in queue:  # grows: an official page's own "Tech Specs" link
        if not budget.can_start("initial_fetch", minimum_seconds=1.0):
            break
        try:
            source = fetch_once(candidate)
        except BudgetExhaustedError:
            break
        fetched.append(source)
        if source.get("status") != "success":
            continue
        # Stage 31.5: search providers do not reliably surface an official
        # product's spec page, but the accepted product page links to it.
        # The linked page is fetched as an unverified-identity official
        # candidate, so its own content must prove the exact model.
        for link in extract_official_spec_links(source, identity):
            key = canonicalize_url(link)
            if key in queued:
                continue
            queued.add(key)
            queue.append({
                **candidate,
                "url": link,
                "discovery_provider": "official_link_expansion",
                "model_match": "unknown",
                "model_relevance": "unknown",
                "identity_relation": "unknown",
                "relevance_reasons": [
                    "Official spec page linked from an accepted product page; "
                    "content verification required."
                ],
            })
        if not budget.can_start("initial_extraction", minimum_seconds=0.05):
            continue
        source_attributes = active.extract(source)
        # Stage 31.5: a side-by-side spec table is read column-bound. The
        # generic table/spec-block readers cannot tell the model columns
        # apart, so once the bound reader claims a page their reads of it are
        # replaced rather than mixed in.
        spec_attributes, spec_diagnostics = extract_official_section_specs(source, identity)
        if spec_diagnostics["outcome"] != "not_applicable":
            spec_table_diagnostics.append({
                "url": str(source.get("final_url") or source.get("source_url") or ""),
                **spec_diagnostics,
            })
        if spec_attributes:
            source_attributes = [
                item for item in source_attributes
                if item.extraction_method not in {"html_table", "spec_block"}
            ]
        extracted.extend(source_attributes)
        extracted.extend(spec_attributes)
        extracted.extend(_official_identity_attributes(
            source,
            identity,
            source_attributes,
        ))
        extracted.extend(extract_official_prose_facts(
            source,
            identity,
            _present_canonical_names([*source_attributes, *spec_attributes]),
        ))
    fetched_sources = tuple(fetched)
    # A column-bound spec-table fact is authoritative over marketing prose
    # (which names some lenses/values, not the full specification).
    bound_labels = {item.name.casefold() for item in extracted if item.model_wide}
    extracted = [
        item for item in extracted
        if not (item.context == PROSE_FACT_CONTEXT and item.name.casefold() in bound_labels)
    ]
    raw_attributes = tuple(extracted)
    category = detect_category(
        identity,
        raw_attributes,
        product_texts=_product_texts(fetched_sources),
    )
    schema = _schema_with_official_extensions(category, raw_attributes)
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
    if request.targeted_search_enabled and budget.can_start(
        "targeted_search", minimum_seconds=1.0,
    ):
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
            budget=budget,
        )

    _resolve_authority_roles(
        [
            *fetched,
            *(
                source
                for field_result in (targeted_search.fields if targeted_search else ())
                for query_result in field_result.query_results
                for source in query_result.fetched_sources
            ),
        ],
        identity.brand,
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
            "targeted_stop_reason": targeted_search.stop_reason if targeted_search else None,
            "targeted_executed_query_count": (
                targeted_search.executed_query_count if targeted_search else 0
            ),
            "targeted_accepted_candidate_count": (
                targeted_search.accepted_candidate_count if targeted_search else 0
            ),
            "targeted_useful_fact_count": (
                targeted_search.useful_fact_count if targeted_search else 0
            ),
            "wall_clock_budget": budget.snapshot(),
            **_official_priority_metadata(
                identity, discovery, selected_candidates, fetched_sources,
                targeted_search, raw_attributes, mapping, schema, validated,
            ),
            "official_spec_table": spec_table_diagnostics,
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
    previous_stage = search.budget_stage
    search.budget_stage = "initial_discovery"
    try:
        return discover_identity_with_status(
            identity,
            market,
            searcher=search.search_with_status,
        )
    finally:
        search.budget_stage = previous_stage
        search.release_transient_resources()


def _discover_targeted_with_released_browser(
    search: ResilientSearchSession,
    identity: ProductIdentity,
    query: str,
    market: str,
) -> DiscoveryOutcome:
    """Release a targeted discovery browser before its candidate is fetched."""
    previous_stage = search.budget_stage
    search.budget_stage = "targeted_discovery"
    try:
        return discover_identity_query_with_status(
            identity,
            query,
            market,
            searcher=search.search_with_status,
        )
    finally:
        search.budget_stage = previous_stage
        search.release_transient_resources()


def run_product_workflow(
    request: ProductWorkflowRequest,
    *,
    services: WorkflowServices | None = None,
) -> ProductWorkflowResult:
    """Run the workflow with one resilient provider session per live request."""
    if services is not None:
        return _run_product_workflow_with_services(request, services)

    budget = WallClockBudget(request.wall_clock_budget_seconds)
    runtime_config = DiscoveryRuntimeConfig(request.provider_timeouts)
    # Opt-in, cross-process provider health (PDV_PROVIDER_HEALTH_PATH); a
    # no-op store when unset, so behavior is unchanged unless a batch runner
    # explicitly enables it for a run. One store instance is resolved here
    # and shared by both discovery (via the session) and fetch below, so a
    # host that discovery already found dead this run and a host fetch finds
    # dead are tracked in the same place.
    health_store = ProviderHealthStore.from_env()
    with ResilientSearchSession(
        request.market,
        config=runtime_config,
        budget=budget,
        health_store=health_store,
    ) as search:
        def live_fetch(candidate: Mapping[str, object]) -> FetchResult:
            # Reserve a small tail for extraction/validation/cleanup and cap
            # any one transport so a blocked host cannot monopolize the run.
            remaining = budget.remaining_seconds
            timeout = budget.timeout_for(
                min(15.0, max(0.0, remaining - 0.75)),
                "fetch",
                minimum_seconds=1.0,
            )
            if timeout is None:
                raise BudgetExhaustedError(
                    budget.exhaustion_reason or "Workflow budget exhausted."
                )
            return fetch_candidate(candidate, timeout=timeout, health_store=health_store)

        live_services = WorkflowServices(
            discover_initial=lambda identity, market: _discover_initial_with_released_browser(
                search, identity, market,
            ),
            discover_targeted=lambda identity, query, market: (
                _discover_targeted_with_released_browser(
                    search, identity, query, market,
                )
            ),
            fetch=live_fetch,
        )
        return _run_product_workflow_with_services(request, live_services, budget=budget)


# Short application-facing alias.
run_workflow = run_product_workflow
