"""Deterministic Stage 6 validation and conflict resolution.

Validation consumes evidence produced by earlier stages. It never searches,
never repairs ambiguous mappings, and never turns a preferred guess into a
confirmed fact.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
import re
import unicodedata
from typing import Iterable, Literal, Mapping

from core.category import CategoryResult
from core.discovery import canonicalize_url
from core.gaps import GapAnalysisResult
from core.identity import ProductIdentity
from core.mapping import CanonicalAttribute, DimensionValue, MappingResult
from core.schema import AttributeDefinition, get_attribute_schema
from core.targeted_search import TargetedSearchResult


ValidationStatus = Literal["Confirmed", "Conflict", "Unresolved"]
ResolutionReason = Literal[
    "official_exact_model_explicit",
    "equivalent_multi_source_evidence",
    "higher_authority_source",
    "specialized_exact_model_explicit",
    "conflicting_equal_authority",
    "insufficient_identity",
    "insufficient_source_quality",
    "missing_provenance",
    "ambiguous_scope",
    "related_evidence_only",
    "blocked_search_only",
    "search_error_only",
    "no_valid_evidence",
]

VARIANT_SENSITIVE_FIELDS = {
    "color", "ram", "storage", "sku", "manufacturer_article", "product_code", "gtin",
    "package_dimensions", "gross_weight",
}

IDENTITY_RANK = {
    "exact_variant": 4,
    "same_base_model": 3,
    "probable_same_base_model": 2,
    "unknown": 1,
    "different_model": 0,
}

MASS_FACTORS = {
    "mg": Decimal("0.001"),
    "g": Decimal("1"),
    "kg": Decimal("1000"),
    "lb": Decimal("453.59237"),
    "oz": Decimal("28.349523125"),
}

LENGTH_FACTORS = {
    "mm": Decimal("1"),
    "cm": Decimal("10"),
    "m": Decimal("1000"),
    "in": Decimal("25.4"),
}

# Tolerances apply after conversion to the documented base units. They are
# deliberately small: one gram for mass and one tenth millimetre for length.
COMPARISON_TOLERANCES = {
    "mass_g": Decimal("1"),
    "length_mm": Decimal("0.1"),
}

PLACEHOLDER_VALUES = {
    "-", "–", "—", "n/a", "na", "not available", "not specified",
    "unknown", "нет данных", "не указано", "невідомо", "не вказано",
}


@dataclass(frozen=True, slots=True)
class CandidateFact:
    """A canonical fact plus the source/identity metadata needed to validate it."""

    attribute: CanonicalAttribute
    authority_status: str = "unknown"
    identity_relation: str = "unknown"
    source_status: str = "success"
    origin: str = "existing"
    source_type: str | None = None

    @property
    def canonical_name(self) -> str:
        return self.attribute.canonical_name

    @property
    def value(self) -> str | DimensionValue:
        return self.attribute.value

    @property
    def source(self) -> str:
        return self.attribute.source_url

    @property
    def evidence(self) -> str:
        return self.attribute.fact_evidence

    @property
    def confidence(self) -> str:
        return self.attribute.mapping_confidence

    @property
    def effective_source_type(self) -> str:
        return self.source_type or self.attribute.source_type or "unknown"


@dataclass(frozen=True, slots=True)
class ValidationDiagnostic:
    code: str
    canonical_name: str
    message: str
    facts: tuple[CandidateFact, ...] = ()


@dataclass(frozen=True, slots=True)
class ValidatedFact:
    canonical_name: str
    value: str | DimensionValue | None
    status: ValidationStatus
    source: str | None
    evidence: str | None
    authority_status: str
    identity_relation: str
    confidence: str
    resolution_reason: ResolutionReason
    supporting_facts: tuple[CandidateFact, ...] = ()
    conflicting_facts: tuple[CandidateFact, ...] = ()

    def __post_init__(self) -> None:
        if self.status == "Confirmed" and not (
            self.value is not None
            and str(self.value).strip()
            and (self.source or "").strip()
            and (self.evidence or "").strip()
            and self.supporting_facts
        ):
            raise ValueError("Confirmed facts require value, source, evidence, and support")


@dataclass(frozen=True, slots=True)
class ValidatedProductProfile:
    identity: ProductIdentity
    category: str
    facts: list[ValidatedFact] = field(default_factory=list)
    unresolved: list[ValidatedFact] = field(default_factory=list)
    conflicts: list[ValidatedFact] = field(default_factory=list)
    diagnostics: list[ValidationDiagnostic] = field(default_factory=list)

    @property
    def confirmed(self) -> list[ValidatedFact]:
        return [fact for fact in self.facts if fact.status == "Confirmed"]

    @property
    def by_name(self) -> dict[str, ValidatedFact]:
        return {fact.canonical_name: fact for fact in self.facts}


@dataclass(frozen=True, slots=True)
class _NormalizedValue:
    kind: str
    values: tuple[Decimal, ...] = ()
    text: str = ""


@dataclass(slots=True)
class _EvidenceGroup:
    normalized: _NormalizedValue
    facts: list[CandidateFact] = field(default_factory=list)


def _category_id(category: str | CategoryResult) -> str:
    return category.category_id if isinstance(category, CategoryResult) else category


def _clean_text(value: object) -> str:
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).split())


def _comparison_text(value: object) -> str:
    text = _clean_text(value).casefold()
    return " ".join(re.findall(r"[\w]+", text))


def _decimal(value: str) -> Decimal | None:
    try:
        return Decimal(value.replace(",", "."))
    except InvalidOperation:
        return None


def _measurement_parts(attribute: CanonicalAttribute) -> tuple[list[Decimal], str | None] | None:
    if isinstance(attribute.value, DimensionValue):
        values = [
            _decimal(attribute.value.height),
            _decimal(attribute.value.width),
            _decimal(attribute.value.depth),
        ]
        if any(value is None for value in values):
            return None
        return [value for value in values if value is not None], attribute.value.unit.casefold()

    value_text = _clean_text(attribute.value)
    unit = _clean_text(attribute.unit).casefold() or None
    if unit is None:
        embedded = re.fullmatch(
            r"(.+?)\s*(mg|kg|g|lb|oz|mm|cm|m|in)",
            value_text,
            re.IGNORECASE,
        )
        if embedded:
            value_text = embedded.group(1)
            unit = embedded.group(2).casefold()
    tokens = re.findall(r"[-+]?\d+(?:[.,]\d+)?", value_text)
    values = [_decimal(token) for token in tokens]
    if not tokens or any(value is None for value in values):
        return None
    return [value for value in values if value is not None], unit


def normalize_value(
    attribute: CanonicalAttribute,
    definition: AttributeDefinition | None,
) -> _NormalizedValue:
    """Return a comparison-only value; the original attribute is never changed."""
    value_type = definition.value_type if definition else "unknown"
    unit_family = definition.unit_family if definition else None
    if value_type == "boolean":
        normalized = _comparison_text(attribute.value)
        truthy = {"1", "yes", "true", "on", "enabled", "да"}
        falsy = {"0", "no", "false", "off", "disabled", "нет"}
        if normalized in truthy:
            return _NormalizedValue("boolean", text="true")
        if normalized in falsy:
            return _NormalizedValue("boolean", text="false")
        return _NormalizedValue("text", text=normalized)

    measurement = _measurement_parts(attribute)
    if value_type == "weight" or unit_family == "mass":
        if measurement and measurement[1] in MASS_FACTORS and len(measurement[0]) == 1:
            return _NormalizedValue(
                "mass_g",
                (measurement[0][0] * MASS_FACTORS[measurement[1]],),
            )
    if value_type == "dimension" or unit_family in {"length", "display_size"}:
        if measurement and measurement[1] in LENGTH_FACTORS:
            factor = LENGTH_FACTORS[measurement[1]]
            return _NormalizedValue(
                "length_mm",
                tuple(value * factor for value in measurement[0]),
            )
    if measurement and measurement[1]:
        return _NormalizedValue(
            f"numeric:{measurement[1]}",
            tuple(measurement[0]),
        )
    return _NormalizedValue("text", text=_comparison_text(attribute.value))


def equivalent_values(left: _NormalizedValue, right: _NormalizedValue) -> bool:
    if left.kind != right.kind:
        return False
    if left.kind == "text" or left.kind == "boolean":
        return left.text == right.text
    if len(left.values) != len(right.values):
        return False
    tolerance = COMPARISON_TOLERANCES.get(left.kind, Decimal("0"))
    return all(abs(a - b) <= tolerance for a, b in zip(left.values, right.values))


_FOREIGN_SCRIPT_RE = re.compile(r"[぀-ヿ㐀-䶿一-鿿가-힣豈-﫿]")


def _concrete_value(value: object) -> bool:
    if isinstance(value, DimensionValue):
        return all(
            _clean_text(item).casefold() not in PLACEHOLDER_VALUES
            and bool(_clean_text(item))
            for item in (
            value.height, value.width, value.depth, value.unit,
            )
        )
    text = _clean_text(value)
    # CJK/Hangul/Kana prose is page furniture (a help-centre sentence mapped
    # onto a canonical alias), never a canonical value for this catalogue.
    return (
        bool(text)
        and text.casefold() not in PLACEHOLDER_VALUES
        and not _FOREIGN_SCRIPT_RE.search(text)
    )


def _authority_rank(fact: CandidateFact) -> int:
    source_type = fact.effective_source_type.casefold()
    if fact.authority_status == "verified" and source_type in {
        "manufacturer", "official_document",
    }:
        return 4
    if source_type in {"specialized_reference", "specialized", "reference"}:
        return 3
    if source_type in {"retailer", "marketplace", "distributor"}:
        return 2
    return 1


def _variant_sensitive(definition: AttributeDefinition | None, canonical_name: str) -> bool:
    return bool(
        canonical_name in VARIANT_SENSITIVE_FIELDS
        or (definition and definition.attribute_scope in {"variant_level", "market_level"})
    )


def _identity_compatible(fact: CandidateFact, variant_sensitive: bool) -> bool:
    if fact.identity_relation == "different_model":
        return False
    if variant_sensitive:
        # A value an official page states for the exact model column without a
        # variant qualifier (Stage 31.5) holds for every variant of that model.
        return fact.identity_relation == "exact_variant" or (
            fact.identity_relation == "same_base_model"
            and bool(getattr(fact.attribute, "model_wide", False))
        )
    return fact.identity_relation in {
        "exact_variant", "same_base_model",
    }


def _provenance_valid(fact: CandidateFact) -> bool:
    return bool(
        fact.source_status == "success"
        and _concrete_value(fact.value)
        and _clean_text(fact.source)
        and _clean_text(fact.evidence)
    )


def _strong_fact(fact: CandidateFact, variant_sensitive: bool) -> bool:
    if not _provenance_valid(fact) or not _identity_compatible(fact, variant_sensitive):
        return False
    authority = _authority_rank(fact)
    if authority == 4:
        return fact.confidence in {"high", "medium"}
    if authority == 3:
        return fact.confidence == "high"
    # Retailer, marketplace, distributor, and generic evidence remain
    # provisional even when several such sources agree.
    return False


def _group_facts(
    facts: Iterable[CandidateFact],
    definition: AttributeDefinition | None,
) -> list[_EvidenceGroup]:
    groups: list[_EvidenceGroup] = []
    for fact in facts:
        normalized = normalize_value(fact.attribute, definition)
        group = next(
            (item for item in groups if equivalent_values(item.normalized, normalized)),
            None,
        )
        if group is None:
            groups.append(_EvidenceGroup(normalized, [fact]))
        else:
            group.facts.append(fact)
    return groups


def _fact_sort_key(fact: CandidateFact) -> tuple[int, int, str, str]:
    return (
        -_authority_rank(fact),
        -IDENTITY_RANK.get(fact.identity_relation, 0),
        canonicalize_url(fact.source) or fact.source,
        _comparison_text(fact.value),
    )


def _deduplicate_candidate_facts(facts: Iterable[CandidateFact]) -> list[CandidateFact]:
    found: list[CandidateFact] = []
    seen: set[tuple[str, str, str, str]] = set()
    for fact in facts:
        key = (
            fact.canonical_name,
            canonicalize_url(fact.source) or fact.source,
            f"{_comparison_text(fact.value)}|{_comparison_text(fact.attribute.unit)}|"
            f"{_comparison_text(fact.attribute.raw_value)}",
            fact.evidence,
        )
        if key not in seen:
            seen.add(key)
            found.append(fact)
    return found


def _source_metadata_for(
    attribute: CanonicalAttribute,
    source_metadata: Mapping[str, Mapping[str, object]] | None,
) -> Mapping[str, object]:
    if not source_metadata:
        return {}
    direct = source_metadata.get(attribute.source_url)
    if direct is not None:
        return direct
    target = canonicalize_url(attribute.source_url)
    return next(
        (
            metadata for url, metadata in source_metadata.items()
            if canonicalize_url(url) == target
        ),
        {},
    )


def _coerce_existing_facts(
    existing: MappingResult | Iterable[CanonicalAttribute | CandidateFact],
    source_metadata: Mapping[str, Mapping[str, object]] | None,
) -> tuple[list[CandidateFact], MappingResult | None]:
    mapping = existing if isinstance(existing, MappingResult) else None
    items: Iterable[CanonicalAttribute | CandidateFact]
    items = mapping.canonical_attributes if mapping is not None else existing
    facts: list[CandidateFact] = []
    for item in items:
        if isinstance(item, CandidateFact):
            facts.append(item)
            continue
        metadata = _source_metadata_for(item, source_metadata)
        facts.append(CandidateFact(
            attribute=item,
            authority_status=str(metadata.get("authority_status") or "unknown"),
            identity_relation=str(metadata.get("identity_relation") or "unknown"),
            source_status=str(metadata.get("status") or "success"),
            origin="existing",
            source_type=str(metadata.get("source_type")) if metadata.get("source_type") else None,
        ))
    return facts, mapping


def _targeted_metadata(
    source_url: str,
    fetched_sources: Iterable[Mapping[str, object]],
    candidates: Iterable[Mapping[str, object]],
) -> Mapping[str, object]:
    target = canonicalize_url(source_url)
    for source in fetched_sources:
        urls = (str(source.get("source_url") or ""), str(source.get("final_url") or ""))
        if any(canonicalize_url(url) == target for url in urls):
            metadata = dict(source)
            metadata.update(source.get("discovery_metadata") or {})
            return metadata
    return next(
        (
            candidate for candidate in candidates
            if canonicalize_url(str(candidate.get("url") or "")) == target
        ),
        {},
    )


def _targeted_facts(targeted: TargetedSearchResult | None) -> list[CandidateFact]:
    if targeted is None:
        return []
    facts: list[CandidateFact] = []
    for field_result in targeted.fields:
        for query_result in field_result.query_results:
            for attribute in query_result.mapped_candidate_facts:
                metadata = _targeted_metadata(
                    attribute.source_url,
                    query_result.fetched_sources,
                    query_result.discovery_candidates,
                )
                facts.append(CandidateFact(
                    attribute=attribute,
                    authority_status=str(metadata.get("authority_status") or "unknown"),
                    identity_relation=str(metadata.get("identity_relation") or "unknown"),
                    source_status=str(metadata.get("status") or "success"),
                    origin="targeted_search",
                    source_type=(
                        str(metadata.get("source_type"))
                        if metadata.get("source_type") else None
                    ),
                ))
    return facts


def _unresolved_reason(
    canonical_name: str,
    candidates: list[CandidateFact],
    mapping: MappingResult | None,
    gap_analysis: GapAnalysisResult | None,
    targeted: TargetedSearchResult | None,
    variant_sensitive: bool,
) -> ResolutionReason:
    field_result = next(
        (item for item in (targeted.fields if targeted else ()) if item.gap.canonical_name == canonical_name),
        None,
    )
    if field_result and not candidates:
        if field_result.search_status == "blocked":
            return "blocked_search_only"
        if field_result.search_status == "error":
            return "search_error_only"
        if any(item.related_candidate_facts for item in field_result.query_results):
            return "related_evidence_only"
    if mapping and any(
        canonical_name in ambiguity.candidates for ambiguity in mapping.ambiguous
    ):
        return "ambiguous_scope"
    if gap_analysis:
        gap = gap_analysis.by_name.get(canonical_name)
        if gap and gap.gap_state == "ambiguous_existing":
            return "ambiguous_scope"
    provenance_valid = [fact for fact in candidates if _provenance_valid(fact)]
    identity_compatible = [
        fact for fact in provenance_valid
        if _identity_compatible(fact, variant_sensitive)
    ]
    if identity_compatible:
        return "insufficient_source_quality"
    if provenance_valid:
        return "insufficient_identity"
    if candidates:
        return "missing_provenance"
    return "no_valid_evidence"


def _resolve_field(
    canonical_name: str,
    candidates: list[CandidateFact],
    definition: AttributeDefinition | None,
    mapping: MappingResult | None,
    gap_analysis: GapAnalysisResult | None,
    targeted: TargetedSearchResult | None,
    diagnostics: list[ValidationDiagnostic],
) -> ValidatedFact:
    variant_sensitive = _variant_sensitive(definition, canonical_name)
    different_model = [fact for fact in candidates if fact.identity_relation == "different_model"]
    if different_model:
        diagnostics.append(ValidationDiagnostic(
            "rejected_wrong_model",
            canonical_name,
            "Different-model evidence was excluded before value grouping.",
            tuple(different_model),
        ))
    technical = [fact for fact in candidates if fact.source_status != "success"]
    if technical:
        diagnostics.append(ValidationDiagnostic(
            "rejected_source_state",
            canonical_name,
            "Blocked/error source results are not facts.",
            tuple(technical),
        ))
    usable = [
        fact for fact in candidates
        if fact.identity_relation != "different_model" and fact.source_status == "success"
    ]
    missing_provenance = [fact for fact in usable if not _provenance_valid(fact)]
    if missing_provenance:
        diagnostics.append(ValidationDiagnostic(
            "rejected_missing_provenance",
            canonical_name,
            "A concrete value, source URL, and evidence are required.",
            tuple(missing_provenance),
        ))
    insufficient_identity = [
        fact for fact in usable
        if _provenance_valid(fact) and not _identity_compatible(fact, variant_sensitive)
    ]
    if insufficient_identity:
        diagnostics.append(ValidationDiagnostic(
            "rejected_insufficient_identity",
            canonical_name,
            "Evidence identity is insufficient for this field's scope.",
            tuple(insufficient_identity),
        ))
    eligible = [
        fact for fact in usable
        if _provenance_valid(fact) and _identity_compatible(fact, variant_sensitive)
    ]
    strong = [fact for fact in usable if _strong_fact(fact, variant_sensitive)]
    strong_groups = _group_facts(strong, definition)

    if strong_groups:
        strongest_authority = max(
            _authority_rank(fact) for group in strong_groups for fact in group.facts
        )
        top_groups = [
            group for group in strong_groups
            if max(_authority_rank(fact) for fact in group.facts) == strongest_authority
        ]
        if len(top_groups) > 1:
            ordered_groups = sorted(
                top_groups,
                key=lambda group: _fact_sort_key(sorted(group.facts, key=_fact_sort_key)[0]),
            )
            all_groups = _group_facts(eligible, definition)
            supporting = tuple(sorted(
                (
                    fact for group in all_groups
                    if equivalent_values(group.normalized, ordered_groups[0].normalized)
                    for fact in group.facts
                ),
                key=_fact_sort_key,
            ))
            conflicting = tuple(
                sorted(
                    (
                        fact for group in all_groups
                        if not equivalent_values(group.normalized, ordered_groups[0].normalized)
                        for fact in group.facts
                    ),
                    key=_fact_sort_key,
                )
            )
            diagnostics.append(ValidationDiagnostic(
                "conflicting_equal_authority",
                canonical_name,
                "Compatible, equally authoritative evidence groups disagree.",
                (*supporting, *conflicting),
            ))
            return ValidatedFact(
                canonical_name, None, "Conflict", None, None,
                supporting[0].authority_status, "mixed", "low",
                "conflicting_equal_authority", supporting, conflicting,
            )

        winner = top_groups[0]
        all_groups = _group_facts(eligible, definition)
        supporting = tuple(sorted(
            (
                fact for group in all_groups
                if equivalent_values(group.normalized, winner.normalized)
                for fact in group.facts
            ),
            key=_fact_sort_key,
        ))
        conflicting = tuple(
            sorted(
                (fact for group in all_groups
                 if not equivalent_values(group.normalized, winner.normalized)
                 for fact in group.facts),
                key=_fact_sort_key,
            )
        )
        primary = supporting[0]
        distinct_sources = {
            canonicalize_url(fact.source) or fact.source for fact in supporting
        }
        if conflicting:
            reason: ResolutionReason = "higher_authority_source"
        elif len(distinct_sources) > 1:
            reason = "equivalent_multi_source_evidence"
        elif strongest_authority == 4:
            reason = "official_exact_model_explicit"
        else:
            reason = "specialized_exact_model_explicit"
        return ValidatedFact(
            canonical_name=canonical_name,
            value=primary.value,
            status="Confirmed",
            source=primary.source,
            evidence=primary.evidence,
            authority_status=primary.authority_status,
            identity_relation=primary.identity_relation,
            confidence="high" if strongest_authority == 4 else "medium",
            resolution_reason=reason,
            supporting_facts=supporting,
            conflicting_facts=conflicting,
        )

    reason = _unresolved_reason(
        canonical_name, usable, mapping, gap_analysis, targeted, variant_sensitive,
    )
    weak = tuple(sorted(eligible, key=_fact_sort_key))
    primary = weak[0] if weak else None
    return ValidatedFact(
        canonical_name=canonical_name,
        value=primary.value if primary else None,
        status="Unresolved",
        source=primary.source if primary else None,
        evidence=primary.evidence if primary else None,
        authority_status=primary.authority_status if primary else "unknown",
        identity_relation=primary.identity_relation if primary else "unknown",
        confidence="low",
        resolution_reason=reason,
        supporting_facts=weak,
    )


def validate_product_profile(
    identity: ProductIdentity,
    category: str | CategoryResult,
    existing_facts: MappingResult | Iterable[CanonicalAttribute | CandidateFact],
    *,
    targeted_search: TargetedSearchResult | None = None,
    gap_analysis: GapAnalysisResult | None = None,
    source_metadata: Mapping[str, Mapping[str, object]] | None = None,
    schema: Iterable[AttributeDefinition] | None = None,
) -> ValidatedProductProfile:
    """Validate all canonical evidence and include every expected field.

    No discovery/fetch/search function is imported or called by this workflow.
    """
    category_id = _category_id(category)
    definitions = list(schema) if schema is not None else get_attribute_schema(category)
    definition_by_name = {item.canonical_name: item for item in definitions}
    existing, mapping = _coerce_existing_facts(existing_facts, source_metadata)
    candidates = _deduplicate_candidate_facts([*existing, *_targeted_facts(targeted_search)])
    by_name: dict[str, list[CandidateFact]] = {}
    for candidate in candidates:
        by_name.setdefault(candidate.canonical_name, []).append(candidate)

    expected_names = [item.canonical_name for item in definitions if item.expected]
    extra_names = sorted(name for name in by_name if name not in expected_names)
    names = [*expected_names, *extra_names]
    diagnostics: list[ValidationDiagnostic] = []
    facts = [
        _resolve_field(
            name,
            by_name.get(name, []),
            definition_by_name.get(name),
            mapping,
            gap_analysis,
            targeted_search,
            diagnostics,
        )
        for name in names
    ]
    unresolved = [fact for fact in facts if fact.status == "Unresolved"]
    conflicts = [fact for fact in facts if fact.status == "Conflict"]
    return ValidatedProductProfile(
        identity=identity,
        category=category_id,
        facts=facts,
        unresolved=unresolved,
        conflicts=conflicts,
        diagnostics=diagnostics,
    )


# Compact compatibility name for callers that prefer the stage operation.
validate_product = validate_product_profile
