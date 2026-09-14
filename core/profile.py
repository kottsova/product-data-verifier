"""Stable Stage 7 final product-profile contract.

This module performs structural transformation and invariant checking only. It
does not search, validate evidence quality, resolve conflicts, or alter a Stage
6 status.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from typing import Iterable, Mapping

from core.category import CATEGORY_NAMES, CategoryResult
from core.identity import ProductIdentity
from core.mapping import DimensionValue
from core.schema import AttributeDefinition, get_attribute_schema
from core.validation import (
    CandidateFact,
    ValidatedFact,
    ValidatedProductProfile,
    ValidationStatus,
)


PROFILE_CONTRACT_VERSION = "1.0"
PUBLIC_STATUSES = {"Confirmed", "Conflict", "Unresolved"}


@dataclass(frozen=True, slots=True)
class RawEvidenceRecord:
    name: str
    value: str
    unit: str | None
    raw_value: str
    source: str
    source_type: str | None
    evidence: str
    extraction_method: str
    confidence: str
    attribute_kind: str
    context: str | None


@dataclass(frozen=True, slots=True)
class EvidenceRecord:
    canonical_name: str
    value: str | DimensionValue
    unit: str | None
    source: str
    source_type: str
    evidence: str
    authority_status: str
    identity_relation: str
    confidence: str
    origin: str
    raw_label: str
    raw_value: str
    mapping_reason: str
    derived: bool
    contributors: tuple[RawEvidenceRecord, ...] = ()


@dataclass(frozen=True, slots=True)
class ProfileAttribute:
    canonical_name: str
    display_name: str
    value: str | DimensionValue | None
    unit: str | None
    status: ValidationStatus
    confidence: str
    source: str | None
    evidence: str | None
    authority_status: str
    identity_relation: str
    resolution_reason: str
    supporting_sources: tuple[EvidenceRecord, ...]
    conflicting_values: tuple[EvidenceRecord, ...]
    expected: bool
    priority: str
    attribute_scope: str
    value_type: str
    unit_family: str | None
    discovered: bool = False


@dataclass(frozen=True, slots=True)
class ProfileCategory:
    category_id: str
    category_name: str
    parent_category: str | None
    confidence: str
    evidence: tuple[str, ...] = ()
    source: str = "unknown"


@dataclass(frozen=True, slots=True)
class ProfileDiagnostic:
    code: str
    canonical_name: str
    message: str
    facts: tuple[EvidenceRecord, ...] = ()


@dataclass(frozen=True, slots=True)
class FinalProductProfile:
    identity: ProductIdentity
    category: ProfileCategory
    attributes: tuple[ProfileAttribute, ...]
    metadata: Mapping[str, object]
    diagnostics: tuple[ProfileDiagnostic, ...] = ()

    @property
    def unresolved(self) -> tuple[ProfileAttribute, ...]:
        return tuple(item for item in self.attributes if item.status == "Unresolved")

    @property
    def conflicts(self) -> tuple[ProfileAttribute, ...]:
        return tuple(item for item in self.attributes if item.status == "Conflict")

    @property
    def discovered(self) -> tuple[ProfileAttribute, ...]:
        return tuple(item for item in self.attributes if item.discovered)

    @property
    def by_name(self) -> dict[str, ProfileAttribute]:
        return {item.canonical_name: item for item in self.attributes}


class ProfileInvariantError(ValueError):
    """Raised when Stage 6 output cannot safely satisfy the public contract."""


def _raw_record(raw: object) -> RawEvidenceRecord:
    return RawEvidenceRecord(
        name=str(getattr(raw, "name", "")),
        value=str(getattr(raw, "value", "")),
        unit=getattr(raw, "unit", None),
        raw_value=str(getattr(raw, "raw_value", "")),
        source=str(getattr(raw, "source_url", "")),
        source_type=getattr(raw, "source_type", None),
        evidence=str(getattr(raw, "evidence", "")),
        extraction_method=str(getattr(raw, "extraction_method", "")),
        confidence=str(getattr(raw, "confidence", "")),
        attribute_kind=str(getattr(raw, "attribute_kind", "")),
        context=getattr(raw, "context", None),
    )


def _evidence_record(fact: CandidateFact) -> EvidenceRecord:
    attribute = fact.attribute
    return EvidenceRecord(
        canonical_name=attribute.canonical_name,
        value=attribute.value,
        unit=attribute.unit,
        source=attribute.source_url,
        source_type=fact.effective_source_type,
        evidence=attribute.fact_evidence,
        authority_status=fact.authority_status,
        identity_relation=fact.identity_relation,
        confidence=fact.confidence,
        origin=fact.origin,
        raw_label=attribute.raw_label,
        raw_value=attribute.raw_value,
        mapping_reason=attribute.mapping_reason,
        derived=attribute.derived,
        contributors=tuple(_raw_record(raw) for raw in attribute.contributors),
    )


def _display_name(canonical_name: str) -> str:
    return canonical_name.replace("_", " ").title()


def _attribute(
    fact: ValidatedFact,
    definition: AttributeDefinition | None,
) -> ProfileAttribute:
    support = tuple(_evidence_record(item) for item in fact.supporting_facts)
    conflicts = tuple(_evidence_record(item) for item in fact.conflicting_facts)
    inferred_scope = next(
        (
            item.attribute.attribute_scope
            for item in (*fact.supporting_facts, *fact.conflicting_facts)
            if item.attribute.attribute_scope != "unknown"
        ),
        "unknown",
    )
    primary_record = next(
        (
            item for item in (*support, *conflicts)
            if item.source == fact.source and item.evidence == fact.evidence
        ),
        support[0] if support else (conflicts[0] if conflicts else None),
    )
    return ProfileAttribute(
        canonical_name=fact.canonical_name,
        display_name=_display_name(fact.canonical_name),
        value=fact.value,
        unit=primary_record.unit if primary_record else None,
        status=fact.status,
        confidence=fact.confidence,
        source=fact.source,
        evidence=fact.evidence,
        authority_status=fact.authority_status,
        identity_relation=fact.identity_relation,
        resolution_reason=fact.resolution_reason,
        supporting_sources=support,
        conflicting_values=conflicts,
        expected=definition.expected if definition else False,
        priority=definition.priority if definition else "low",
        attribute_scope=definition.attribute_scope if definition else inferred_scope,
        value_type=definition.value_type if definition else "unknown",
        unit_family=definition.unit_family if definition else None,
        discovered=definition is None,
    )


def _category(
    validated: ValidatedProductProfile,
    category_result: CategoryResult | None,
) -> ProfileCategory:
    if category_result is not None:
        if category_result.category_id != validated.category:
            raise ProfileInvariantError("category result does not match validated profile")
        return ProfileCategory(
            category_id=category_result.category_id,
            category_name=category_result.category_name,
            parent_category=category_result.parent_category,
            confidence=category_result.confidence,
            evidence=tuple(category_result.evidence),
            source=category_result.source,
        )
    name, parent = CATEGORY_NAMES.get(validated.category, (validated.category, None))
    return ProfileCategory(
        category_id=validated.category,
        category_name=name,
        parent_category=parent,
        confidence="low",
        evidence=(),
        source="unknown",
    )


def _diagnostic(diagnostic: object) -> ProfileDiagnostic:
    return ProfileDiagnostic(
        code=str(getattr(diagnostic, "code", "")),
        canonical_name=str(getattr(diagnostic, "canonical_name", "")),
        message=str(getattr(diagnostic, "message", "")),
        facts=tuple(
            _evidence_record(fact) for fact in getattr(diagnostic, "facts", ())
        ),
    )


def _attribute_order(
    attribute: ProfileAttribute,
    definitions: Mapping[str, tuple[int, AttributeDefinition]],
) -> tuple[int, int, str]:
    located = definitions.get(attribute.canonical_name)
    if located is None:
        return 2, 0, attribute.canonical_name
    index, definition = located
    return (0 if definition.expected else 1), index, attribute.canonical_name


def _source_count(attributes: Iterable[ProfileAttribute]) -> int:
    sources: set[str] = set()
    for attribute in attributes:
        if attribute.source:
            sources.add(attribute.source)
        sources.update(item.source for item in attribute.supporting_sources if item.source)
        sources.update(item.source for item in attribute.conflicting_values if item.source)
    return len(sources)


def _value_signature(
    value: str | DimensionValue,
    unit: str | None,
) -> tuple[object, ...]:
    if isinstance(value, DimensionValue):
        return ("dimension", value.height, value.width, value.depth, value.unit)
    return ("scalar", str(value), unit)


def validate_final_profile(
    profile: FinalProductProfile,
    *,
    expected_names: Iterable[str] | None = None,
) -> None:
    """Check export structure without reconsidering any Stage 6 decision."""
    names: set[str] = set()
    for attribute in profile.attributes:
        if not attribute.canonical_name.strip():
            raise ProfileInvariantError("canonical_name is required")
        if attribute.canonical_name in names:
            raise ProfileInvariantError(
                f"duplicate canonical attribute: {attribute.canonical_name}"
            )
        names.add(attribute.canonical_name)
        if attribute.status not in PUBLIC_STATUSES:
            raise ProfileInvariantError(
                f"unsupported public status for {attribute.canonical_name}: {attribute.status}"
            )
        if attribute.status == "Confirmed":
            if attribute.value is None or not str(attribute.value).strip():
                raise ProfileInvariantError(
                    f"Confirmed attribute {attribute.canonical_name} lacks a value"
                )
            if not (attribute.source or "").strip():
                raise ProfileInvariantError(
                    f"Confirmed attribute {attribute.canonical_name} lacks a source"
                )
            if not (attribute.evidence or "").strip():
                raise ProfileInvariantError(
                    f"Confirmed attribute {attribute.canonical_name} lacks evidence"
                )
            if not attribute.supporting_sources:
                raise ProfileInvariantError(
                    f"Confirmed attribute {attribute.canonical_name} lacks supporting provenance"
                )
        elif attribute.status == "Conflict":
            if not attribute.supporting_sources or not attribute.conflicting_values:
                raise ProfileInvariantError(
                    f"Conflict attribute {attribute.canonical_name} requires two evidence groups"
                )
            supporting = {
                _value_signature(item.value, item.unit)
                for item in attribute.supporting_sources
            }
            conflicting = {
                _value_signature(item.value, item.unit)
                for item in attribute.conflicting_values
            }
            if not supporting or not conflicting or not (conflicting - supporting):
                raise ProfileInvariantError(
                    f"Conflict attribute {attribute.canonical_name} lacks distinct alternatives"
                )

        for evidence in (*attribute.supporting_sources, *attribute.conflicting_values):
            if evidence.canonical_name != attribute.canonical_name:
                raise ProfileInvariantError(
                    f"provenance field mismatch for {attribute.canonical_name}"
                )
            if not isinstance(evidence.source, str) or not isinstance(evidence.evidence, str):
                raise ProfileInvariantError(
                    f"non-serializable provenance for {attribute.canonical_name}"
                )
            if attribute.status in {"Confirmed", "Conflict"} and not (
                evidence.source.strip() and evidence.evidence.strip()
            ):
                raise ProfileInvariantError(
                    f"{attribute.status} provenance is incomplete for {attribute.canonical_name}"
                )

    required = set(expected_names or ())
    missing = sorted(required - names)
    if missing:
        raise ProfileInvariantError(
            f"expected attributes are missing from final profile: {', '.join(missing)}"
        )
    try:
        json.dumps(dict(profile.metadata), ensure_ascii=False)
    except (TypeError, ValueError) as error:
        raise ProfileInvariantError("profile metadata is not JSON serializable") from error


def build_final_profile(
    validated: ValidatedProductProfile,
    *,
    category_result: CategoryResult | None = None,
    schema: Iterable[AttributeDefinition] | None = None,
    metadata: Mapping[str, object] | None = None,
) -> FinalProductProfile:
    """Transform Stage 6 output into the stable Stage 7 profile contract."""
    definitions_list = list(schema) if schema is not None else get_attribute_schema(
        validated.category
    )
    definitions = {
        item.canonical_name: (index, item)
        for index, item in enumerate(definitions_list)
    }
    attributes = [
        _attribute(fact, definitions.get(fact.canonical_name, (0, None))[1])
        for fact in validated.facts
    ]
    attributes.sort(key=lambda item: _attribute_order(item, definitions))
    confirmed_count = sum(item.status == "Confirmed" for item in attributes)
    conflict_count = sum(item.status == "Conflict" for item in attributes)
    unresolved_count = sum(item.status == "Unresolved" for item in attributes)
    discovered_count = sum(item.discovered for item in attributes)
    profile_metadata: dict[str, object] = {
        "contract_version": PROFILE_CONTRACT_VERSION,
        "attribute_count": len(attributes),
        "confirmed_count": confirmed_count,
        "conflict_count": conflict_count,
        "unresolved_count": unresolved_count,
        "discovered_count": discovered_count,
        "source_count": _source_count(attributes),
    }
    if metadata:
        for key in sorted(metadata):
            if key in profile_metadata:
                raise ProfileInvariantError(f"reserved profile metadata key: {key}")
            profile_metadata[key] = metadata[key]
    profile = FinalProductProfile(
        identity=validated.identity,
        category=_category(validated, category_result),
        attributes=tuple(attributes),
        metadata=profile_metadata,
        diagnostics=tuple(_diagnostic(item) for item in validated.diagnostics),
    )
    expected_names = [item.canonical_name for item in definitions_list if item.expected]
    validate_final_profile(profile, expected_names=expected_names)
    return profile


# Concise alias for callers treating Stage 7 as finalization.
finalize_profile = build_final_profile
