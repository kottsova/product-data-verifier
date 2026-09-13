"""Deterministic Stage 5 gap classification and search eligibility."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Literal

from core.category import CategoryResult
from core.identity import ProductIdentity
from core.mapping import AmbiguousMapping, MappingResult
from core.schema import (
    AttributeDefinition,
    Priority,
    SchemaDiagnostics,
    analyze_schema_coverage,
    get_attribute_schema,
    resolve_attribute_definition,
)


GapState = Literal[
    "satisfied",
    "ambiguous_existing",
    "missing_searchable",
    "missing_low_priority",
    "unsupported_or_not_applicable",
]

PRIORITY_ORDER: dict[Priority, int] = {
    "critical": 0,
    "high": 1,
    "medium": 2,
    "low": 3,
}

# These are semantic relationships, not equivalences. A fact in the same
# family is useful context, but never satisfies another canonical field.
RELATED_FIELDS: tuple[frozenset[str], ...] = (
    frozenset({"net_weight", "gross_weight", "weight"}),
    frozenset({"product_dimensions", "package_dimensions", "dimensions"}),
)

PRECISE_SEARCH_INTENTS: dict[str, str] = {
    "net_weight": "net weight",
    "gross_weight": "gross weight",
    "product_dimensions": "product dimensions",
    "package_dimensions": "package dimensions",
    "ip_rating": "IP rating",
}

IDENTITY_FIELDS = {
    "brand", "model", "manufacturer_article", "product_code", "sku", "gtin",
}


@dataclass(frozen=True, slots=True)
class Gap:
    canonical_name: str
    priority: Priority
    gap_state: GapState
    reason: str
    category: str
    known_related_facts: tuple[object, ...] = ()
    ambiguity_context: tuple[str, ...] = ()
    suggested_search_intent: str | None = None
    targeted_search_allowed: bool = False
    attribute_scope: str = "unknown"
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class GapAnalysisResult:
    category: str
    gaps: list[Gap] = field(default_factory=list)
    minimum_search_priority: Priority = "medium"
    coverage: SchemaDiagnostics | None = None

    @property
    def searchable(self) -> list[Gap]:
        return [gap for gap in self.gaps if gap.targeted_search_allowed]

    @property
    def by_name(self) -> dict[str, Gap]:
        return {gap.canonical_name: gap for gap in self.gaps}


def _category_id(category: str | CategoryResult) -> str:
    return category.category_id if isinstance(category, CategoryResult) else category


def _related_names(canonical_name: str) -> frozenset[str]:
    return next(
        (family for family in RELATED_FIELDS if canonical_name in family),
        frozenset({canonical_name}),
    )


def _ambiguities_for(
    canonical_name: str,
    ambiguous: Iterable[AmbiguousMapping],
) -> list[AmbiguousMapping]:
    return [item for item in ambiguous if canonical_name in item.candidates]


def _known_related_facts(
    definition: AttributeDefinition,
    mapping: MappingResult,
    coverage: SchemaDiagnostics,
) -> tuple[object, ...]:
    names = _related_names(definition.canonical_name)
    facts: list[object] = []
    for name in names:
        facts.extend(coverage.present.get(name, ()))
    for ambiguity in mapping.ambiguous:
        if names.intersection(ambiguity.candidates):
            facts.append(ambiguity.raw_attribute)
    tokens = {
        "weight" if "weight" in definition.canonical_name else "",
        "dimension" if "dimension" in definition.canonical_name else "",
    } - {""}
    for raw in mapping.unmapped:
        label = raw.name.casefold()
        if any(token in label for token in tokens):
            facts.append(raw)
    return tuple(dict.fromkeys(facts))


def _search_intent(definition: AttributeDefinition) -> str:
    return PRECISE_SEARCH_INTENTS.get(
        definition.canonical_name,
        definition.canonical_name.replace("_", " "),
    )


def _has_variant_signal(identity: ProductIdentity | None, canonical_name: str) -> bool:
    if identity is None:
        return False
    if canonical_name in {"ram", "storage"}:
        return bool(identity.configuration.get(canonical_name))
    if canonical_name == "color":
        return bool(identity.color)
    return bool(
        identity.variant_suffix
        or identity.manufacturer_article
        or identity.product_code
        or identity.sku
        or identity.gtin
    )


def _independently_searchable(
    definition: AttributeDefinition,
    identity: ProductIdentity | None,
    ambiguities: list[AmbiguousMapping],
) -> tuple[bool, str | None]:
    if definition.canonical_name in IDENTITY_FIELDS:
        return False, "Identity facts must be resolved by ProductIdentity, not field search."
    if identity is None or not (identity.commercial_model or identity.base_model):
        return False, "A resolved model identity is required for targeted search."
    if definition.canonical_name == "color" and any(
        "display" in (item.raw_attribute.context or "").casefold()
        or "screen" in (item.raw_attribute.context or "").casefold()
        for item in ambiguities
    ):
        return False, "Display color ambiguity is not evidence of product variant color."
    if definition.attribute_scope == "variant_level" and not _has_variant_signal(
        identity, definition.canonical_name,
    ):
        return False, "Variant-level field lacks a stable variant identity signal."
    return True, None


def _deterministic_mapping_issues(
    canonical_name: str,
    category: str | CategoryResult,
    mapping: MappingResult,
) -> tuple[object, ...]:
    issues: list[object] = []
    for raw in mapping.unmapped:
        definition = resolve_attribute_definition(raw.name, category)
        if definition and definition.canonical_name == canonical_name:
            issues.append(raw)
    return tuple(issues)


def analyze_gaps(
    category: str | CategoryResult,
    mapping: MappingResult,
    *,
    identity: ProductIdentity | None = None,
    coverage: SchemaDiagnostics | None = None,
    schema: Iterable[AttributeDefinition] | None = None,
    minimum_search_priority: Priority = "medium",
) -> GapAnalysisResult:
    """Classify schema fields without treating related or ambiguous facts as coverage."""
    if minimum_search_priority not in PRIORITY_ORDER:
        raise ValueError(f"unsupported minimum_search_priority: {minimum_search_priority}")
    category_id = _category_id(category)
    definitions = list(schema) if schema is not None else get_attribute_schema(category)
    diagnostics = coverage or analyze_schema_coverage(category, mapping, identity=identity)
    gaps: list[Gap] = []

    for definition in definitions:
        name = definition.canonical_name
        ambiguities = _ambiguities_for(name, mapping.ambiguous)
        known = _known_related_facts(definition, mapping, diagnostics)
        ambiguity_context = tuple(
            dict.fromkeys(
                f"{item.raw_attribute.name}={item.raw_attribute.raw_value}; "
                f"candidates={','.join(item.candidates)}; reason={item.reason}"
                for item in ambiguities
            )
        )
        intent: str | None = None
        allowed = False
        mapping_issues = _deterministic_mapping_issues(name, category, mapping)

        if name in diagnostics.present:
            state: GapState = "satisfied"
            reason = "Canonical evidence already satisfies the field."
        elif not definition.expected:
            state = "unsupported_or_not_applicable"
            reason = "Field is present in the layered schema but is not expected for this category."
        elif ambiguities:
            state = "ambiguous_existing"
            intent = _search_intent(definition)
            safe, unsafe_reason = _independently_searchable(definition, identity, ambiguities)
            high_enough = PRIORITY_ORDER[definition.priority] <= PRIORITY_ORDER[minimum_search_priority]
            allowed = safe and high_enough
            reason = (
                "Related raw evidence exists but its scope does not establish this canonical field."
                + (f" {unsafe_reason}" if unsafe_reason else "")
                + (" Priority is below the configured search threshold." if not high_enough else "")
            )
        elif PRIORITY_ORDER[definition.priority] > PRIORITY_ORDER[minimum_search_priority]:
            state = "missing_low_priority"
            reason = "Expected field is missing but below the configured search priority threshold."
        else:
            state = "missing_searchable"
            intent = _search_intent(definition)
            safe, unsafe_reason = _independently_searchable(definition, identity, ambiguities)
            allowed = safe and not mapping_issues
            reason = "Expected canonical field has no current evidence."
            if unsafe_reason:
                reason += f" {unsafe_reason}"
            if mapping_issues:
                reason += " An unambiguously resolvable raw fact indicates a deterministic mapping issue; search is suppressed."

        gaps.append(Gap(
            canonical_name=name,
            priority=definition.priority,
            gap_state=state,
            reason=reason,
            category=category_id,
            known_related_facts=known,
            ambiguity_context=ambiguity_context,
            suggested_search_intent=intent,
            targeted_search_allowed=allowed,
            attribute_scope=definition.attribute_scope,
            aliases=tuple(definition.aliases),
        ))

    return GapAnalysisResult(category_id, gaps, minimum_search_priority, diagnostics)
