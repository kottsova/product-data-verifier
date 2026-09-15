"""Stage 10 stable application service boundary.

This module is the only thing a consumer (CLI, future Telegram bot, future
API) should depend on to verify a product. It wraps the existing pipeline
(``core.workflow.run_product_workflow``) and the Stage 9 quality gate
(``core.quality.assess_product_quality``) behind a small, stable,
JSON-serializable request/result contract.

It does not perform discovery, fetch, extraction, mapping, validation, or
quality scoring itself -- it only calls the existing stages once and reshapes
their already-decided output. It never changes a fact, a validation status,
an authority decision, or a quality threshold.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Literal, Mapping

from core.identity import ProductIdentity
from core.mapping import DimensionValue
from core.profile import EvidenceRecord, FinalProductProfile, ProfileAttribute, ProfileCategory
from core.quality import QualityAssessment, assess_product_quality
from core.workflow import (
    ProductWorkflowRequest,
    ProductWorkflowResult,
    run_product_workflow,
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

    def to_dict(self) -> dict[str, object]:
        return {
            "brand": self.brand,
            "model": self.model,
            "article": self.article,
            "market": self.market,
            "max_sources": self.max_sources,
            "targeted_search_enabled": self.targeted_search_enabled,
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
    quality: QualityAssessment | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)
    error: VerifyProductError | None = None

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
        }


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
        quality=quality,
        metadata=dict(profile.metadata),
    )


WorkflowRunner = Callable[[ProductWorkflowRequest], ProductWorkflowResult]


class ProductVerifierService:
    """Instantiable service boundary. Not a global singleton.

    ``run_workflow`` is injectable so tests can substitute a deterministic
    fake and never touch the network; it defaults to the real pipeline.
    """

    def __init__(self, *, run_workflow: WorkflowRunner = run_product_workflow) -> None:
        self._run_workflow = run_workflow

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
        """
        try:
            internal_request = _build_internal_request(request)
        except ValueError as error:
            return _failure(request, "invalid_request", str(error)), None

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
            return _to_result(request, workflow_result), workflow_result
        except Exception as error:  # noqa: BLE001 - a mapping bug must not crash the boundary
            return _failure(
                request, "internal_error", f"Unexpected failure while building the result: {error}",
                detail=type(error).__name__,
            ), None


def verify_product(
    request: VerifyProductRequest,
    *,
    service: ProductVerifierService | None = None,
) -> VerifyProductResult:
    """Module-level convenience entry point. Builds a fresh service per call."""
    active = service or ProductVerifierService()
    return active.verify(request)
