"""Stage 10 stable application service boundary, with Stage 11 caching.

This module is the only thing a consumer (CLI, future Telegram bot, future
API) should depend on to verify a product. It wraps the existing pipeline
(``core.workflow.run_product_workflow``) and the Stage 9 quality gate
(``core.quality.assess_product_quality``) behind a small, stable,
JSON-serializable request/result contract, with an optional persistent cache
(``services.cache``) in front of the live pipeline.

It does not perform discovery, fetch, extraction, mapping, validation, or
quality scoring itself -- it only calls the existing stages once and reshapes
their already-decided output. It never changes a fact, a validation status,
an authority decision, or a quality threshold. Caching is opt-in: with no
repository injected, behavior is identical to Stage 10 (always live).
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import hashlib
import json
import time
from typing import Callable, Literal, Mapping
import unicodedata

from core.identity import ProductIdentity
from core.mapping import DimensionValue
from core.profile import EvidenceRecord, FinalProductProfile, ProfileAttribute, ProfileCategory
from core.quality import QualityAssessment, assess_product_quality
from core.workflow import (
    ProductWorkflowRequest,
    ProductWorkflowResult,
    run_product_workflow,
)
from services.cache import (
    CACHE_SCHEMA_VERSION,
    CacheEntry,
    CachePolicy,
    DEFAULT_CACHE_POLICY,
    ProductVerificationRepository,
)


ErrorKind = Literal["invalid_request", "workflow_failure", "internal_error"]
_ERROR_KINDS = {"invalid_request", "workflow_failure", "internal_error"}


@dataclass(frozen=True, slots=True)
class VerifyProductRequest:
    """Minimal, stable input contract. No internal Stage structures inside."""

    brand: str
    model: str
    article: str | None = None
    market: str = "global"
    max_sources: int = 5
    targeted_search_enabled: bool = True
    # Stage 11: bypass a cache hit and force a live re-run. Never part of the
    # cache key -- the refreshed result is still saved for later requests.
    force_refresh: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "brand": self.brand,
            "model": self.model,
            "article": self.article,
            "market": self.market,
            "max_sources": self.max_sources,
            "targeted_search_enabled": self.targeted_search_enabled,
            "force_refresh": self.force_refresh,
        }


@dataclass(frozen=True, slots=True)
class VerifyProductError:
    """Explicit, typed application-level error. Never an internal exception."""

    kind: ErrorKind
    message: str
    detail: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in _ERROR_KINDS:
            raise ValueError(f"unsupported error kind: {self.kind}")

    def to_dict(self) -> dict[str, object]:
        return {"kind": self.kind, "message": self.message, "detail": self.detail}


@dataclass(frozen=True, slots=True)
class ServiceEvidence:
    """Public, flattened provenance record. Mirrors EvidenceRecord's public fields."""

    value: object
    unit: str | None
    source: str
    source_type: str
    evidence: str
    authority_status: str
    confidence: str
    origin: str

    def to_dict(self) -> dict[str, object]:
        return {
            "value": self.value,
            "unit": self.unit,
            "source": self.source,
            "source_type": self.source_type,
            "evidence": self.evidence,
            "authority_status": self.authority_status,
            "confidence": self.confidence,
            "origin": self.origin,
        }


@dataclass(frozen=True, slots=True)
class ServiceAttribute:
    """Public, stable view of one final-profile attribute plus its provenance."""

    canonical_name: str
    display_name: str
    value: object
    unit: str | None
    status: str
    confidence: str
    source: str | None
    evidence: str | None
    priority: str
    expected: bool
    discovered: bool
    supporting_sources: tuple[ServiceEvidence, ...] = ()
    conflicting_values: tuple[ServiceEvidence, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "canonical_name": self.canonical_name,
            "display_name": self.display_name,
            "value": self.value,
            "unit": self.unit,
            "status": self.status,
            "confidence": self.confidence,
            "source": self.source,
            "evidence": self.evidence,
            "priority": self.priority,
            "expected": self.expected,
            "discovered": self.discovered,
            "supporting_sources": [item.to_dict() for item in self.supporting_sources],
            "conflicting_values": [item.to_dict() for item in self.conflicting_values],
        }


@dataclass(frozen=True, slots=True)
class ServiceQuality:
    """Public, stable view of the Stage 9 QualityAssessment.

    Mirrors every field of core.quality.QualityAssessment 1:1 -- this is a
    reshaping boundary only. It never recomputes or reinterprets the Stage 9
    verdict, thresholds, or reasons; it just stops the internal
    QualityAssessment type itself from crossing the service boundary.
    """

    status: str
    coverage_percent: float
    schema_total: int
    schema_found: int
    confirmed_count: int
    unresolved_count: int
    conflict_count: int
    critical_total: int
    critical_found: int
    critical_confirmed: int
    critical_conflict: int
    critical_high_authority_confirmed: int
    category_confidence: str
    identity_confidence: str
    reasons: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "coverage_percent": self.coverage_percent,
            "schema_total": self.schema_total,
            "schema_found": self.schema_found,
            "confirmed_count": self.confirmed_count,
            "unresolved_count": self.unresolved_count,
            "conflict_count": self.conflict_count,
            "critical_total": self.critical_total,
            "critical_found": self.critical_found,
            "critical_confirmed": self.critical_confirmed,
            "critical_conflict": self.critical_conflict,
            "critical_high_authority_confirmed": self.critical_high_authority_confirmed,
            "category_confidence": self.category_confidence,
            "identity_confidence": self.identity_confidence,
            "reasons": list(self.reasons),
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True, slots=True)
class ServiceCategory:
    category_id: str
    category_name: str
    parent_category: str | None
    confidence: str

    def to_dict(self) -> dict[str, object]:
        return {
            "category_id": self.category_id,
            "category_name": self.category_name,
            "parent_category": self.parent_category,
            "confidence": self.confidence,
        }


@dataclass(frozen=True, slots=True)
class ServiceIdentity:
    """Public, normalized product identity (a stable subset of ProductIdentity)."""

    brand: str
    base_model: str | None
    commercial_model: str | None
    manufacturer_article: str | None
    product_code: str | None
    sku: str | None
    gtin: str | None
    color: str | None
    configuration: Mapping[str, str]
    confidence: str

    def to_dict(self) -> dict[str, object]:
        return {
            "brand": self.brand,
            "base_model": self.base_model,
            "commercial_model": self.commercial_model,
            "manufacturer_article": self.manufacturer_article,
            "product_code": self.product_code,
            "sku": self.sku,
            "gtin": self.gtin,
            "color": self.color,
            "configuration": dict(self.configuration),
            "confidence": self.confidence,
        }


@dataclass(frozen=True, slots=True)
class VerifyProductResult:
    """Stable output contract. The only thing a consumer needs to import."""

    success: bool
    request: VerifyProductRequest
    identity: ServiceIdentity | None = None
    category: ServiceCategory | None = None
    attributes: tuple[ServiceAttribute, ...] = ()
    unresolved: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = ()
    discovered: tuple[str, ...] = ()
    quality: ServiceQuality | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)
    error: VerifyProductError | None = None
    # Stage 11: backward-compatible cache metadata (all default to "not
    # cached" so a live result looks identical to Stage 10's shape plus
    # these three extra keys).
    served_from_cache: bool = False
    cache_stored_at: float | None = None
    cache_age_seconds: float | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "success": self.success,
            "request": self.request.to_dict(),
            "identity": self.identity.to_dict() if self.identity else None,
            "category": self.category.to_dict() if self.category else None,
            "attributes": [item.to_dict() for item in self.attributes],
            "unresolved": list(self.unresolved),
            "conflicts": list(self.conflicts),
            "discovered": list(self.discovered),
            "quality": self.quality.to_dict() if self.quality else None,
            "metadata": dict(self.metadata),
            "error": self.error.to_dict() if self.error else None,
            "served_from_cache": self.served_from_cache,
            "cache_stored_at": self.cache_stored_at,
            "cache_age_seconds": self.cache_age_seconds,
        }


# --- Stage 11: reconstruct the stable DTO tree from its own to_dict() shape.
# Used only to restore a cached VerifyProductResult; never touches core.*.


def _service_evidence_from_dict(data: Mapping[str, object]) -> ServiceEvidence:
    return ServiceEvidence(
        value=data["value"],
        unit=data["unit"],
        source=data["source"],
        source_type=data["source_type"],
        evidence=data["evidence"],
        authority_status=data["authority_status"],
        confidence=data["confidence"],
        origin=data["origin"],
    )


def _service_attribute_from_dict(data: Mapping[str, object]) -> ServiceAttribute:
    return ServiceAttribute(
        canonical_name=data["canonical_name"],
        display_name=data["display_name"],
        value=data["value"],
        unit=data["unit"],
        status=data["status"],
        confidence=data["confidence"],
        source=data["source"],
        evidence=data["evidence"],
        priority=data["priority"],
        expected=data["expected"],
        discovered=data["discovered"],
        supporting_sources=tuple(
            _service_evidence_from_dict(item) for item in data["supporting_sources"]
        ),
        conflicting_values=tuple(
            _service_evidence_from_dict(item) for item in data["conflicting_values"]
        ),
    )


def _service_category_from_dict(data: Mapping[str, object]) -> ServiceCategory:
    return ServiceCategory(
        category_id=data["category_id"],
        category_name=data["category_name"],
        parent_category=data["parent_category"],
        confidence=data["confidence"],
    )


def _service_identity_from_dict(data: Mapping[str, object]) -> ServiceIdentity:
    return ServiceIdentity(
        brand=data["brand"],
        base_model=data["base_model"],
        commercial_model=data["commercial_model"],
        manufacturer_article=data["manufacturer_article"],
        product_code=data["product_code"],
        sku=data["sku"],
        gtin=data["gtin"],
        color=data["color"],
        configuration=dict(data["configuration"]),
        confidence=data["confidence"],
    )


def _service_quality_from_dict(data: Mapping[str, object]) -> ServiceQuality:
    return ServiceQuality(
        status=data["status"],
        coverage_percent=data["coverage_percent"],
        schema_total=data["schema_total"],
        schema_found=data["schema_found"],
        confirmed_count=data["confirmed_count"],
        unresolved_count=data["unresolved_count"],
        conflict_count=data["conflict_count"],
        critical_total=data["critical_total"],
        critical_found=data["critical_found"],
        critical_confirmed=data["critical_confirmed"],
        critical_conflict=data["critical_conflict"],
        critical_high_authority_confirmed=data["critical_high_authority_confirmed"],
        category_confidence=data["category_confidence"],
        identity_confidence=data["identity_confidence"],
        reasons=tuple(data["reasons"]),
        warnings=tuple(data["warnings"]),
    )


def _verify_error_from_dict(data: Mapping[str, object]) -> VerifyProductError:
    return VerifyProductError(kind=data["kind"], message=data["message"], detail=data["detail"])


def _verify_request_from_dict(data: Mapping[str, object]) -> VerifyProductRequest:
    return VerifyProductRequest(
        brand=data["brand"],
        model=data["model"],
        article=data["article"],
        market=data["market"],
        max_sources=data["max_sources"],
        targeted_search_enabled=data["targeted_search_enabled"],
        force_refresh=bool(data.get("force_refresh", False)),
    )


def _result_from_dict(data: Mapping[str, object]) -> VerifyProductResult:
    """Reverse of VerifyProductResult.to_dict(). Raises on malformed input --
    callers must treat that as an unusable cache entry, never a live error."""
    return VerifyProductResult(
        success=data["success"],
        request=_verify_request_from_dict(data["request"]),
        identity=_service_identity_from_dict(data["identity"]) if data.get("identity") else None,
        category=_service_category_from_dict(data["category"]) if data.get("category") else None,
        attributes=tuple(
            _service_attribute_from_dict(item) for item in data.get("attributes", ())
        ),
        unresolved=tuple(data.get("unresolved", ())),
        conflicts=tuple(data.get("conflicts", ())),
        discovered=tuple(data.get("discovered", ())),
        quality=_service_quality_from_dict(data["quality"]) if data.get("quality") else None,
        metadata=dict(data.get("metadata", {})),
        error=_verify_error_from_dict(data["error"]) if data.get("error") else None,
    )


def _normalize_key_part(value: object) -> str:
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).split()).casefold()


def _cache_key(request: VerifyProductRequest) -> str:
    """Deterministic key over exactly the fields that can change the result.

    ``force_refresh`` is deliberately excluded: it controls whether *this*
    call reads the cache, not which entry the result belongs to.
    """
    parts = {
        "brand": _normalize_key_part(request.brand),
        "model": _normalize_key_part(request.model),
        "article": _normalize_key_part(request.article),
        "market": _normalize_key_part(request.market),
        "max_sources": request.max_sources,
        "targeted_search_enabled": request.targeted_search_enabled,
    }
    canonical = json.dumps(parts, sort_keys=True, ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _serialize_value(value: object) -> object:
    if isinstance(value, DimensionValue):
        return {
            "height": value.height, "width": value.width,
            "depth": value.depth, "unit": value.unit,
        }
    return value


def _service_evidence(record: EvidenceRecord) -> ServiceEvidence:
    return ServiceEvidence(
        value=_serialize_value(record.value),
        unit=record.unit,
        source=record.source,
        source_type=record.source_type,
        evidence=record.evidence,
        authority_status=record.authority_status,
        confidence=record.confidence,
        origin=record.origin,
    )


def _service_attribute(attribute: ProfileAttribute) -> ServiceAttribute:
    return ServiceAttribute(
        canonical_name=attribute.canonical_name,
        display_name=attribute.display_name,
        value=_serialize_value(attribute.value),
        unit=attribute.unit,
        status=attribute.status,
        confidence=attribute.confidence,
        source=attribute.source,
        evidence=attribute.evidence,
        priority=attribute.priority,
        expected=attribute.expected,
        discovered=attribute.discovered,
        supporting_sources=tuple(_service_evidence(item) for item in attribute.supporting_sources),
        conflicting_values=tuple(_service_evidence(item) for item in attribute.conflicting_values),
    )


def _service_quality(quality: QualityAssessment) -> ServiceQuality:
    return ServiceQuality(
        status=quality.status,
        coverage_percent=quality.coverage_percent,
        schema_total=quality.schema_total,
        schema_found=quality.schema_found,
        confirmed_count=quality.confirmed_count,
        unresolved_count=quality.unresolved_count,
        conflict_count=quality.conflict_count,
        critical_total=quality.critical_total,
        critical_found=quality.critical_found,
        critical_confirmed=quality.critical_confirmed,
        critical_conflict=quality.critical_conflict,
        critical_high_authority_confirmed=quality.critical_high_authority_confirmed,
        category_confidence=quality.category_confidence,
        identity_confidence=quality.identity_confidence,
        reasons=tuple(quality.reasons),
        warnings=tuple(quality.warnings),
    )


def _service_category(category: ProfileCategory) -> ServiceCategory:
    return ServiceCategory(
        category_id=category.category_id,
        category_name=category.category_name,
        parent_category=category.parent_category,
        confidence=category.confidence,
    )


def _service_identity(identity: ProductIdentity) -> ServiceIdentity:
    return ServiceIdentity(
        brand=identity.brand,
        base_model=identity.base_model,
        commercial_model=identity.commercial_model,
        manufacturer_article=identity.manufacturer_article,
        product_code=identity.product_code,
        sku=identity.sku,
        gtin=identity.gtin,
        color=identity.color,
        configuration=dict(identity.configuration),
        confidence=identity.confidence,
    )


def _build_internal_request(request: VerifyProductRequest) -> ProductWorkflowRequest:
    """Reuse Stage 8's own validation rather than re-implementing input checks."""
    return ProductWorkflowRequest.from_parts(
        request.brand,
        request.model,
        request.article,
        market=request.market,
        max_initial_sources=request.max_sources,
        targeted_search_enabled=request.targeted_search_enabled,
    )


def _failure(
    request: VerifyProductRequest,
    kind: ErrorKind,
    message: str,
    *,
    detail: str | None = None,
) -> VerifyProductResult:
    return VerifyProductResult(
        success=False,
        request=request,
        error=VerifyProductError(kind=kind, message=message, detail=detail),
    )


def _to_result(
    request: VerifyProductRequest,
    workflow_result: ProductWorkflowResult,
) -> VerifyProductResult:
    profile: FinalProductProfile = workflow_result.final_profile
    quality = getattr(workflow_result, "quality", None) or assess_product_quality(profile)
    return VerifyProductResult(
        success=True,
        request=request,
        identity=_service_identity(profile.identity),
        category=_service_category(profile.category),
        attributes=tuple(_service_attribute(item) for item in profile.attributes),
        unresolved=tuple(item.canonical_name for item in profile.unresolved),
        conflicts=tuple(item.canonical_name for item in profile.conflicts),
        discovered=tuple(item.canonical_name for item in profile.discovered),
        quality=_service_quality(quality),
        metadata=dict(profile.metadata),
    )


WorkflowRunner = Callable[[ProductWorkflowRequest], ProductWorkflowResult]
Clock = Callable[[], float]


class ProductVerifierService:
    """Instantiable service boundary. Not a global singleton.

    ``run_workflow`` is injectable so tests can substitute a deterministic
    fake and never touch the network; it defaults to the real pipeline.

    Caching is entirely opt-in: with ``repository=None`` (the default),
    behavior is identical to Stage 10 -- every call runs the live pipeline.
    Passing a ``ProductVerificationRepository`` turns on the cache lookup/
    save flow; ``clock`` is injectable so freshness (TTL) is deterministic
    in tests.
    """

    def __init__(
        self,
        *,
        run_workflow: WorkflowRunner = run_product_workflow,
        repository: ProductVerificationRepository | None = None,
        cache_policy: CachePolicy = DEFAULT_CACHE_POLICY,
        clock: Clock = time.time,
    ) -> None:
        self._run_workflow = run_workflow
        self._repository = repository
        self._cache_policy = cache_policy
        self._clock = clock

    def verify(self, request: VerifyProductRequest) -> VerifyProductResult:
        result, _ = self.verify_with_workflow_result(request)
        return result

    def verify_with_workflow_result(
        self, request: VerifyProductRequest,
    ) -> tuple[VerifyProductResult, ProductWorkflowResult | None]:
        """Return the stable result alongside the raw internal workflow result.

        This is a deliberate, narrow escape hatch for first-party, same-
        codebase callers (currently only the CLI) that still need the full
        internal profile to keep an existing legacy output format (table/CSV)
        byte-for-byte unchanged. It is NOT part of the stable public
        contract: a future consumer (Telegram bot, API) must depend on
        ``verify()`` / ``VerifyProductResult`` only, and must never import
        ``ProductWorkflowResult``.

        On a cache hit, no pipeline ran, so the raw workflow result is
        ``None`` -- only the CLI's no-cache configuration ever relies on the
        second element being non-``None`` for a successful result.
        """
        try:
            internal_request = _build_internal_request(request)
        except ValueError as error:
            return _failure(request, "invalid_request", str(error)), None

        cache_key = self._cache_key_for(request)
        if cache_key is not None and not request.force_refresh:
            cached = self._lookup_cache(cache_key, request)
            if cached is not None:
                return cached, None

        try:
            workflow_result = self._run_workflow(internal_request)
        except (RuntimeError, ValueError) as error:
            return _failure(
                request, "workflow_failure", str(error),
                detail=type(error).__name__,
            ), None
        except Exception as error:  # noqa: BLE001 - boundary must not leak exception types
            return _failure(
                request, "internal_error", f"Unexpected failure: {error}",
                detail=type(error).__name__,
            ), None

        try:
            result = _to_result(request, workflow_result)
        except Exception as error:  # noqa: BLE001 - a mapping bug must not crash the boundary
            return _failure(
                request, "internal_error", f"Unexpected failure while building the result: {error}",
                detail=type(error).__name__,
            ), None

        if cache_key is not None:
            self._store_cache(cache_key, result)
        return result, workflow_result

    def _cache_key_for(self, request: VerifyProductRequest) -> str | None:
        return _cache_key(request) if self._repository is not None else None

    def _lookup_cache(
        self, key: str, request: VerifyProductRequest,
    ) -> VerifyProductResult | None:
        assert self._repository is not None
        try:
            entry = self._repository.get(key)
        except Exception:  # noqa: BLE001 - a broken store degrades to a live run
            return None
        if entry is None or entry.schema_version != CACHE_SCHEMA_VERSION:
            return None
        if not entry.success and not self._cache_policy.cache_failures:
            return None
        try:
            cached_result = _result_from_dict(entry.payload)
        except Exception:  # noqa: BLE001 - corrupted payload -> unusable, not a crash
            return None
        now = self._clock()
        age = max(0.0, now - entry.stored_at)
        if age > self._cache_policy.ttl_seconds:
            return None
        return replace(
            cached_result,
            request=request,
            served_from_cache=True,
            cache_stored_at=entry.stored_at,
            cache_age_seconds=age,
        )

    def _store_cache(self, key: str, result: VerifyProductResult) -> None:
        assert self._repository is not None
        if not result.success and not self._cache_policy.cache_failures:
            return
        try:
            self._repository.save(CacheEntry(
                key=key,
                schema_version=CACHE_SCHEMA_VERSION,
                stored_at=self._clock(),
                success=result.success,
                payload=result.to_dict(),
            ))
        except Exception:  # noqa: BLE001 - caching is best-effort
            pass


def verify_product(
    request: VerifyProductRequest,
    *,
    service: ProductVerifierService | None = None,
) -> VerifyProductResult:
    """Module-level convenience entry point. Builds a fresh service per call."""
    active = service or ProductVerifierService()
    return active.verify(request)
