"""Conservative, provenance-preserving mapping of raw attributes to schema fields."""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Iterable, Literal

from core.category import CategoryResult
from core.extract import RawAttribute
from core.normalize import attribute_label_variants, normalize_attribute_label
from core.schema import AttributeDefinition, get_attribute_schema


MappingConfidence = Literal["high", "medium", "low"]


@dataclass(frozen=True, slots=True)
class DimensionValue:
    height: str
    width: str
    depth: str
    unit: str


@dataclass(frozen=True, slots=True)
class CanonicalAttribute:
    canonical_name: str
    value: str | DimensionValue
    unit: str | None
    raw_label: str
    raw_value: str
    source_url: str
    source_type: str | None
    fact_evidence: str
    mapping_confidence: MappingConfidence
    mapping_reason: str
    derived: bool = False
    contributors: tuple[RawAttribute, ...] = ()
    attribute_scope: str = "unknown"
    context: str | None = None


@dataclass(frozen=True, slots=True)
class AmbiguousMapping:
    raw_attribute: RawAttribute
    candidates: tuple[str, ...]
    reason: str


@dataclass(frozen=True, slots=True)
class MappingResult:
    raw_attributes: list[RawAttribute] = field(default_factory=list)
    mapped: list[CanonicalAttribute] = field(default_factory=list)
    unmapped: list[RawAttribute] = field(default_factory=list)
    ambiguous: list[AmbiguousMapping] = field(default_factory=list)
    derived: list[CanonicalAttribute] = field(default_factory=list)

    @property
    def canonical_attributes(self) -> list[CanonicalAttribute]:
        return [*self.mapped, *self.derived]


AMBIGUOUS_LABELS = {
    "weight", "dimensions", "dimension", "capacity", "size", "color", "colour",
    "объем", "емкость",
}
BATTERY_CONTEXT = re.compile(r"\bbattery\b|\baccumulator\b|аккумулятор|батаре|ელემენტ|აკუმულატორ", re.I)
DISPLAY_CONTEXT = re.compile(r"\bdisplay\b|\bscreen\b|экран|диспле|ეკრან", re.I)
PACKAGE_CONTEXT = re.compile(r"\bpackag(?:e|ing|ed)\b|\bbox\b|shipping|упаков|შეფუთ", re.I)
PRODUCT_DIMENSION_CONTEXT = re.compile(
    r"\b(?:product|item|device|appliance)\b|physical dimensions|dimensions and weight|"
    r"габариты изделия|размеры прибора|корпус|მოწყობილობ",
    re.I,
)
COMPONENTS = {"height", "width", "depth"}
UNIT_ALIASES = {
    "mm": "mm", "millimeter": "mm", "millimeters": "mm",
    "millimetre": "mm", "millimetres": "mm",
    "cm": "cm", "centimeter": "cm", "centimeters": "cm",
    "centimetre": "cm", "centimetres": "cm",
    "m": "m", "meter": "m", "meters": "m", "metre": "m", "metres": "m",
    "in": "in", "inch": "in", "inches": "in",
    "g": "g", "gram": "g", "grams": "g",
    "kg": "kg", "kilogram": "kg", "kilograms": "kg",
    "w": "W", "kw": "kW", "v": "V", "hz": "Hz", "mah": "mAh",
    "мм": "mm", "см": "cm", "м": "m",
    "г": "g", "кг": "kg", "мл": "ml", "л": "L",
    "вт": "W", "квт": "kW", "в": "V", "гц": "Hz",
    "па": "Pa", "кпа": "kPa",
    "мач": "mAh", "ма год": "mAh", "ач": "Ah", "а год": "Ah",
    "мин": "min", "хв": "min", "ч": "h", "год": "h",
    "дб": "dB", "db": "dB",
}


def _key(value: str | None) -> str:
    return normalize_attribute_label(value)


def _clean(value: str | None) -> str:
    return " ".join((value or "").split())


def _unit(value: str | None) -> str | None:
    key = _key(value)
    return UNIT_ALIASES.get(key, _clean(value) or None)


def _schema(
    schema: Iterable[AttributeDefinition] | None,
    category: str | CategoryResult | None,
) -> list[AttributeDefinition]:
    if schema is not None:
        return list(schema)
    return get_attribute_schema(category or "unknown")


def _alias_index(
    definitions: Iterable[AttributeDefinition],
) -> dict[str, list[AttributeDefinition]]:
    index: dict[str, list[AttributeDefinition]] = {}
    for definition in definitions:
        for alias in (definition.canonical_name, *definition.aliases):
            for key in attribute_label_variants(alias):
                index.setdefault(key, []).append(definition)
    return index


def _canonical(
    raw: RawAttribute,
    definition: AttributeDefinition,
    confidence: MappingConfidence,
    reason: str,
) -> CanonicalAttribute:
    return CanonicalAttribute(
        canonical_name=definition.canonical_name,
        value=_clean(raw.value),
        unit=_unit(raw.unit),
        raw_label=raw.name,
        raw_value=raw.raw_value,
        source_url=raw.source_url,
        source_type=raw.source_type,
        fact_evidence=raw.evidence,
        mapping_confidence=confidence,
        mapping_reason=reason,
        derived=False,
        contributors=(raw,),
        attribute_scope=definition.attribute_scope,
        context=raw.context,
    )


def _contextual_target(raw: RawAttribute, definitions: dict[str, AttributeDefinition]) -> tuple[str, str] | None:
    label = _key(raw.name)
    context = _clean(raw.context)
    if label == "capacity" and BATTERY_CONTEXT.search(context) and "battery_capacity" in definitions:
        return "battery_capacity", "contextual:battery_section+capacity"
    if label == "size" and DISPLAY_CONTEXT.search(context) and "display_size" in definitions:
        return "display_size", "contextual:display_section+size"
    if label == "type" and DISPLAY_CONTEXT.search(context) and "display_type" in definitions:
        return "display_type", "contextual:display_section+type"
    if label == "weight" and PACKAGE_CONTEXT.search(context) and "gross_weight" in definitions:
        return "gross_weight", "contextual:packaging_section+weight"
    if label in {"dimensions", "dimension"}:
        if PACKAGE_CONTEXT.search(context) and "package_dimensions" in definitions:
            return "package_dimensions", "contextual:packaging_section+dimensions"
        if PRODUCT_DIMENSION_CONTEXT.search(context) and "product_dimensions" in definitions:
            return "product_dimensions", "contextual:product_section+dimensions"
    return None


def _ambiguity_candidates(label: str, definitions: dict[str, AttributeDefinition]) -> tuple[str, ...]:
    possible = {
        "weight": ("net_weight", "gross_weight", "weight"),
        "dimensions": ("product_dimensions", "package_dimensions", "dimensions"),
        "dimension": ("product_dimensions", "package_dimensions", "dimensions"),
        "capacity": ("battery_capacity", "capacity", "clean_water_tank", "dirty_water_tank"),
        "объем": ("battery_capacity", "capacity", "clean_water_tank", "dirty_water_tank"),
        "емкость": ("battery_capacity", "capacity", "clean_water_tank", "dirty_water_tank"),
        "size": ("display_size", "product_dimensions"),
        "color": ("color",),
        "colour": ("color",),
    }.get(label, ())
    return tuple(name for name in possible if name in definitions)


def _dimension_component(raw: RawAttribute) -> tuple[str, str, str] | None:
    label = _key(raw.name)
    words = label.split()
    component = next((word for word in reversed(words) if word in COMPONENTS), None)
    if component is None:
        return None
    explicit_scope: str | None = None
    if PACKAGE_CONTEXT.search(label):
        explicit_scope = "package_dimensions"
    elif PRODUCT_DIMENSION_CONTEXT.search(label):
        explicit_scope = "product_dimensions"

    context = _clean(raw.context)
    target = explicit_scope
    if target is None and PACKAGE_CONTEXT.search(context):
        target = "package_dimensions"
    elif target is None and PRODUCT_DIMENSION_CONTEXT.search(context):
        target = "product_dimensions"
    if target is None:
        return component, "", _key(context)
    context_key = _key(context) or f"explicit {target}"
    return component, target, context_key


def _compose_dimensions(
    components: list[tuple[RawAttribute, str, str, str]],
    definitions: dict[str, AttributeDefinition],
) -> tuple[list[CanonicalAttribute], set[int]]:
    groups: dict[tuple[str, str, str], dict[str, list[RawAttribute]]] = {}
    for raw, component, target, context_key in components:
        if not target or target not in definitions:
            continue
        key = (raw.source_url, target, context_key)
        groups.setdefault(key, {}).setdefault(component, []).append(raw)

    derived: list[CanonicalAttribute] = []
    used: set[int] = set()
    for (_source, target, _context_key), by_component in groups.items():
        if set(by_component) != COMPONENTS or any(len(items) != 1 for items in by_component.values()):
            continue
        contributors = tuple(by_component[name][0] for name in ("height", "width", "depth"))
        units = {_unit(item.unit) for item in contributors}
        if None in units or len(units) != 1:
            continue
        unit = next(iter(units))
        explicit = all(
            (target == "package_dimensions" and PACKAGE_CONTEXT.search(_key(item.name)))
            or (target == "product_dimensions" and PRODUCT_DIMENSION_CONTEXT.search(_key(item.name)))
            for item in contributors
        )
        confidence: MappingConfidence = "high" if explicit else "medium"
        context = contributors[0].context
        derived.append(CanonicalAttribute(
            canonical_name=target,
            value=DimensionValue(
                height=_clean(contributors[0].value),
                width=_clean(contributors[1].value),
                depth=_clean(contributors[2].value),
                unit=unit,
            ),
            unit=unit,
            raw_label="Height + Width + Depth",
            raw_value=" × ".join(item.raw_value for item in contributors),
            source_url=contributors[0].source_url,
            source_type=contributors[0].source_type,
            fact_evidence=" | ".join(dict.fromkeys(item.evidence for item in contributors)),
            mapping_confidence=confidence,
            mapping_reason=f"composed:height+width+depth:{target}",
            derived=True,
            contributors=contributors,
            attribute_scope=definitions[target].attribute_scope,
            context=context,
        ))
        used.update(id(item) for item in contributors)
    return derived, used


def map_attributes(
    raw_attributes: Iterable[RawAttribute],
    schema: Iterable[AttributeDefinition] | None = None,
    category: str | CategoryResult | None = None,
) -> MappingResult:
    """Map raw facts additively; uncertainty remains explicit and inspectable."""
    raw_items = list(raw_attributes)
    schema_items = _schema(schema, category)
    definitions = {item.canonical_name: item for item in schema_items}
    aliases = _alias_index(schema_items)
    mapped: list[CanonicalAttribute] = []
    unmapped: list[RawAttribute] = []
    ambiguous: list[AmbiguousMapping] = []
    component_items: list[tuple[RawAttribute, str, str, str]] = []
    pending: list[RawAttribute] = []

    for raw in raw_items:
        component = _dimension_component(raw)
        if component and _key(raw.name).split()[-1] in COMPONENTS:
            component_items.append((raw, *component))
            continue
        labels = attribute_label_variants(raw.name)
        label = labels[0] if labels else ""
        contextual = _contextual_target(raw, definitions)
        if contextual:
            target, reason = contextual
            mapped.append(_canonical(raw, definitions[target], "medium", reason))
            continue
        ambiguous_label = next((item for item in labels if item in AMBIGUOUS_LABELS), None)
        if ambiguous_label:
            candidates = _ambiguity_candidates(ambiguous_label, definitions)
            if candidates:
                ambiguous.append(AmbiguousMapping(raw, candidates, f"ambiguous_label:{ambiguous_label}"))
            else:
                unmapped.append(raw)
            continue
        matches = {
            item.canonical_name: item
            for candidate_label in labels
            for item in aliases.get(candidate_label, [])
        }
        if len(matches) == 1:
            definition = next(iter(matches.values()))
            if definition.canonical_name in {"product_dimensions", "package_dimensions", "net_weight", "gross_weight"}:
                reason = f"explicit_scope:{definition.canonical_name}"
            else:
                direct_matches = {
                    item.canonical_name
                    for item in aliases.get(label, [])
                }
                prefix = "exact_alias" if definition.canonical_name in direct_matches else "normalized_alias"
                reason = f"{prefix}:{definition.canonical_name}"
            mapped.append(_canonical(raw, definition, "high", reason))
        elif len(matches) > 1:
            ambiguous.append(AmbiguousMapping(raw, tuple(matches), "ambiguous_alias_collision"))
        else:
            pending.append(raw)

    derived, used_components = _compose_dimensions(component_items, definitions)
    for raw, _component, target, _context_key in component_items:
        if id(raw) in used_components:
            continue
        candidates = (target,) if target else tuple(
            name for name in ("product_dimensions", "package_dimensions") if name in definitions
        )
        if candidates:
            reason = "incomplete_or_incompatible_dimension_components" if target else "ambiguous_dimension_scope"
            ambiguous.append(AmbiguousMapping(raw, candidates, reason))
        else:
            unmapped.append(raw)
    unmapped.extend(pending)
    return MappingResult(raw_items, mapped, unmapped, ambiguous, derived)
