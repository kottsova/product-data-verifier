"""Compact layered attribute schemas for detected product categories."""

from __future__ import annotations

from dataclasses import dataclass, field
import re
import unicodedata
from typing import Any, Iterable, Literal, Protocol

from core.category import CATEGORY_NAMES, CategoryResult
from core.identity import AttributeScope


SchemaScope = Literal["universal", "category_specific", "discovered"]
ValueType = Literal[
    "text", "integer", "number", "boolean", "dimension", "weight", "power",
    "voltage", "frequency", "capacity", "duration", "count", "enum", "list",
    "unknown",
]
Priority = Literal["critical", "high", "medium", "low"]

SCHEMA_SCOPES = {"universal", "category_specific", "discovered"}
VALUE_TYPES = {
    "text", "integer", "number", "boolean", "dimension", "weight", "power",
    "voltage", "frequency", "capacity", "duration", "count", "enum", "list",
    "unknown",
}
ATTRIBUTE_SCOPES = {"model_level", "variant_level", "market_level", "unknown"}
PRIORITIES = {"critical", "high", "medium", "low"}


class AttributeLike(Protocol):
    name: str


@dataclass(frozen=True, slots=True)
class AttributeDefinition:
    canonical_name: str
    aliases: list[str] = field(default_factory=list)
    scope: SchemaScope = "category_specific"
    value_type: ValueType = "unknown"
    unit_family: str | None = None
    attribute_scope: AttributeScope = "unknown"
    priority: Priority = "medium"
    expected: bool = True
    notes: str | None = None

    def __post_init__(self) -> None:
        if not self.canonical_name.strip():
            raise ValueError("canonical_name is required")
        if self.scope not in SCHEMA_SCOPES:
            raise ValueError(f"unsupported schema scope: {self.scope}")
        if self.value_type not in VALUE_TYPES:
            raise ValueError(f"unsupported value_type: {self.value_type}")
        if self.attribute_scope not in ATTRIBUTE_SCOPES:
            raise ValueError(f"unsupported attribute_scope: {self.attribute_scope}")
        if self.priority not in PRIORITIES:
            raise ValueError(f"unsupported priority: {self.priority}")
        object.__setattr__(self, "aliases", list(dict.fromkeys(self.aliases)))


@dataclass(frozen=True, slots=True)
class SchemaDiagnostics:
    present: dict[str, list[object]] = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)
    discovered: list[object] = field(default_factory=list)


def _definition(
    canonical_name: str,
    aliases: tuple[str, ...] = (),
    *,
    scope: SchemaScope = "category_specific",
    value_type: ValueType = "unknown",
    unit_family: str | None = None,
    attribute_scope: AttributeScope = "unknown",
    priority: Priority = "medium",
    expected: bool = True,
    notes: str | None = None,
) -> AttributeDefinition:
    return AttributeDefinition(
        canonical_name, list(aliases), scope, value_type, unit_family,
        attribute_scope, priority, expected, notes,
    )


UNIVERSAL_ATTRIBUTES = (
    _definition("brand", ("brand", "бренд", "ბრენდი"), scope="universal", value_type="text",
                priority="critical"),
    _definition("model", ("model", "model number", "модель"), scope="universal",
                value_type="text", priority="critical"),
    _definition("manufacturer_article", ("manufacturer article", "mpn", "артикул"),
                scope="universal", value_type="text", attribute_scope="market_level",
                priority="high", expected=False),
    _definition("product_code", ("product code", "код товара", "პროდუქტის კოდი"),
                scope="universal", value_type="text", attribute_scope="variant_level",
                priority="high", expected=False),
    _definition("sku", ("sku", "stock keeping unit"), scope="universal",
                value_type="text", attribute_scope="market_level", priority="high",
                expected=False),
    _definition("gtin", ("gtin", "ean", "upc", "barcode"), scope="universal",
                value_type="text", attribute_scope="market_level", priority="high",
                expected=False),
    _definition("color", ("colour", "color", "цвет"), scope="universal",
                value_type="enum", attribute_scope="variant_level", priority="medium",
                expected=False),
    _definition("country_of_origin", ("country of origin", "country of production",
                "страна производства", "made in"), scope="universal", value_type="text",
                priority="medium", expected=False),
    _definition("warranty", ("warranty", "warranty period", "гарантия"),
                scope="universal", value_type="duration", attribute_scope="market_level",
                priority="medium", expected=False),
    _definition("product_dimensions", ("product dimensions", "item dimensions", "device dimensions", "appliance dimensions",
                "dimensions of the product", "dimensions of the product hxwxd",
                "габариты изделия", "размеры прибора"), scope="universal",
                value_type="dimension", unit_family="length", priority="high"),
    _definition("package_dimensions", ("package dimensions", "packaging dimensions",
                "dimensions of the packed product", "dimensions of packed product",
                "dimensions of the packed product hxwxd",
                "габариты упаковки", "размеры упаковки"), scope="universal",
                value_type="dimension", unit_family="length", attribute_scope="market_level",
                priority="high"),
    _definition("net_weight", ("net weight", "product net weight", "вес нетто"),
                scope="universal", value_type="weight", unit_family="mass",
                priority="high"),
    _definition("gross_weight", ("gross weight", "package weight", "packaging weight", "packaged weight", "shipping weight",
                "вес брутто", "вес с упаковкой"), scope="universal", value_type="weight",
                unit_family="mass", attribute_scope="market_level", priority="high"),
    _definition("package_contents", ("package contents", "box contents", "комплектация"),
                scope="universal", value_type="list", priority="medium", expected=False),
)


CATEGORY_ATTRIBUTES: dict[str, tuple[AttributeDefinition, ...]] = {
    "cooktop": (
        _definition("heating_type", ("heating type", "hob type", "cooktop type", "тип нагрева"), value_type="enum", priority="high"),
        _definition("number_of_zones", ("number of zones", "number of cooking zones", "number of electric cooking zones", "cooking zones", "total number of positions that can be used at the same time", "количество конфорок"), value_type="count", priority="critical"),
        _definition("zone_diameter", ("zone diameter", "cooking zone diameter", "диаметр конфорки"), value_type="dimension", unit_family="length", priority="high"),
        _definition("zone_power", ("zone power", "cooking zone power", "мощность конфорки"), value_type="power", unit_family="power", priority="high"),
        _definition("connection_rating", ("connection rating", "connected load", "connection power", "мощность подключения"), value_type="power", unit_family="power", priority="critical"),
        _definition("voltage", ("voltage", "напряжение"), value_type="voltage", unit_family="voltage", priority="high"),
        _definition("frequency", ("frequency", "частота"), value_type="frequency", unit_family="frequency", priority="medium"),
        _definition("installation_dimensions", ("installation dimensions", "required niche size for installation", "required niche size for installation hxwxd", "размеры ниши", "монтажные размеры"), value_type="dimension", unit_family="length", priority="high"),
        _definition("surface_material", ("surface material", "basic surface material", "surface type", "материал поверхности"), value_type="enum", priority="medium"),
        _definition("control_type", ("control type", "type of control", "управление"), value_type="enum", priority="medium"),
        _definition("timer", ("timer", "таймер"), value_type="boolean", priority="medium"),
        _definition("safety_features", ("safety features", "child lock", "residual heat indicator", "защитные функции"), value_type="list", priority="high"),
    ),
    "smartphone": (
        _definition("display_size", ("display size", "screen size", "screen diagonal"), value_type="dimension", unit_family="display_size", priority="critical"),
        _definition("display_type", ("display type", "screen type", "panel type"), value_type="enum", priority="high"),
        _definition("display_resolution", ("display resolution", "screen resolution", "resolution"), value_type="dimension", unit_family="pixels", priority="high"),
        _definition("refresh_rate", ("refresh rate",), value_type="frequency", unit_family="frequency", priority="medium"),
        _definition("processor", ("processor", "cpu", "cpu model", "chipset"), value_type="text", attribute_scope="model_level", priority="critical"),
        _definition("gpu", ("gpu", "graphics processor"), value_type="text", attribute_scope="model_level", priority="medium"),
        _definition("ram", ("ram", "memory ram"), value_type="capacity", unit_family="digital_storage", attribute_scope="variant_level", priority="critical"),
        _definition("storage", ("storage", "internal storage", "rom"), value_type="capacity", unit_family="digital_storage", attribute_scope="variant_level", priority="critical"),
        _definition("rear_camera", ("rear camera", "main camera"), value_type="text", priority="high"),
        _definition("front_camera", ("front camera", "selfie camera"), value_type="text", priority="high"),
        _definition("battery_capacity", ("battery capacity", "typical battery capacity", "rated battery capacity"), value_type="capacity", unit_family="electric_charge", priority="critical"),
        _definition("charging_power", ("charging power", "wired charging", "charger power"), value_type="power", unit_family="power", attribute_scope="variant_level", priority="high"),
        _definition("wifi", ("wifi", "wi-fi", "wlan"), value_type="text", priority="high"),
        _definition("bluetooth", ("bluetooth",), value_type="text", priority="high"),
        _definition("sim", ("sim", "sim card", "sim card 1", "sim card 2", "sim type"), value_type="list", attribute_scope="variant_level", priority="high"),
        _definition("ip_rating", ("ip rating", "ingress protection"), value_type="enum", priority="medium"),
        _definition("color", ("colour", "color", "цвет"), value_type="enum", attribute_scope="variant_level", priority="high"),
    ),
    "sewing_machine": (
        _definition("machine_type", ("machine type", "тип машины"), value_type="enum", priority="critical"),
        _definition("shuttle_type", ("shuttle type", "hook type", "тип челнока"), value_type="enum", priority="critical"),
        _definition("operation_count", ("operation count", "number of operations", "количество операций"), value_type="count", priority="critical"),
        _definition("buttonhole_type", ("buttonhole type", "buttonhole", "выполнение петли"), value_type="enum", priority="high"),
        _definition("stitch_length", ("stitch length", "maximum stitch length", "макс длина стежка мм", "максимальная длина стежка мм", "длина стежка"), value_type="dimension", unit_family="length", priority="high"),
        _definition("stitch_width", ("stitch width", "maximum stitch width", "макс ширина стежка мм", "максимальная ширина стежка мм", "ширина стежка"), value_type="dimension", unit_family="length", priority="high"),
        _definition("presser_foot_lift", ("presser foot lift", "maximum presser foot lift", "макс высота подъема лапки мм", "максимальная высота подъема лапки мм"), value_type="dimension", unit_family="length", priority="medium"),
        _definition("reverse", ("reverse", "reverse sewing", "реверс"), value_type="boolean", priority="medium"),
        _definition("lighting", ("lighting", "подсветка", "подсветка рабочей поверхности"), value_type="text", priority="medium"),
        _definition("thread_cutter", ("thread cutter", "устройство обрезки нити"), value_type="text", priority="medium"),
        _definition("free_arm", ("free arm", "съемная рукавная платформа"), value_type="boolean", priority="medium"),
        _definition("power", ("power", "мощность вт", "мощность"), value_type="power", unit_family="power", priority="high"),
        _definition("accessories", ("accessories", "standard accessories", "стандартная комплектация", "лапки в комплекте"), value_type="list", priority="medium"),
    ),
    "air_fryer": (
        _definition("power", ("power", "мощность"), value_type="power", unit_family="power", priority="critical"),
        _definition("capacity", ("capacity", "bowl capacity", "capacity of each bowl", "объем каждой чаши", "объем чаши", "емкость чаш"), value_type="capacity", unit_family="volume", priority="critical"),
        _definition("number_of_bowls", ("number of bowls", "количество чаш"), value_type="count", priority="high"),
        _definition("program_count", ("program count", "number of programs", "количество программ"), value_type="count", priority="high"),
        _definition("temperature_range", ("temperature range", "temperature", "температура", "регулировка температуры"), value_type="text", unit_family="temperature", priority="high"),
        _definition("timer_range", ("timer range", "timer", "таймер"), value_type="duration", unit_family="time", priority="high"),
        _definition("control_type", ("control type", "control", "управление"), value_type="enum", priority="medium"),
        _definition("bowl_coating", ("bowl coating", "non-stick coating", "антипригарное покрытие", "покрытие чаш"), value_type="text", priority="medium"),
        _definition("dimensions", ("dimensions", "габариты", "габариты ш в г"), value_type="dimension", unit_family="length", priority="high", notes="Generic product dimensions; never infer package dimensions from this label."),
        _definition("weight", ("weight", "вес"), value_type="weight", unit_family="mass", priority="high", notes="Generic product weight; never infer gross weight from this label."),
        _definition("cord_length", ("cord length", "длина сетевого шнура", "длина сетевого кабеля", "длина кабеля"), value_type="dimension", unit_family="length", priority="medium"),
    ),
    "wet_dry_vacuum": (
        _definition("suction_power", ("suction power", "suction pressure", "мощность всасывания", "შესრუტვის სიმძლავრე 25 000 პა 4 სამუშაო რეჟიმი", "შესრუტვის მაქსიმალური სიმძლავრე კპა"), value_type="power", unit_family="pressure_or_power", priority="critical"),
        _definition("rated_power", ("rated power", "power", "номинальная мощность", "სიმძლავრე"), value_type="power", unit_family="power", priority="high"),
        _definition("battery_capacity", ("battery capacity", "აკუმულატორის ტევადობა"), value_type="capacity", unit_family="electric_charge", attribute_scope="variant_level", priority="critical"),
        _definition("runtime", ("runtime", "run time", "operating time", "время работы", "ავტონომიური მუშაობის დრო"), value_type="duration", unit_family="time", priority="critical"),
        _definition("charging_time", ("charging time", "charge time", "время зарядки", "დატენვის დრო"), value_type="duration", unit_family="time", priority="critical"),
        _definition("clean_water_tank", ("clean water tank", "clean water tank capacity", "бак для чистой воды", "სუფთა წყლის კონტეინერის მოცულობა ლ"), value_type="capacity", unit_family="volume", priority="critical"),
        _definition("dirty_water_tank", ("dirty water tank", "dirty water tank capacity", "бак для грязной воды", "ჭუჭყიანი წყლის კონტეინერის მოცულობა ლ"), value_type="capacity", unit_family="volume", priority="critical"),
        _definition("modes", ("modes", "cleaning modes", "режимы", "წმენდის რეჟიმების რაოდენობა"), value_type="list", priority="high"),
        _definition("self_cleaning", ("self cleaning", "self-cleaning", "самоочистка"), value_type="boolean", priority="high"),
        _definition("drying_temperature", ("drying temperature", "температура сушки"), value_type="number", unit_family="temperature", priority="medium"),
        _definition("weight", ("weight", "вес", "წონა"), value_type="weight", unit_family="mass", priority="high", notes="Generic product weight; never infer gross weight from this label."),
        _definition("dimensions", ("dimensions", "габариты"), value_type="dimension", unit_family="length", priority="high", notes="Generic product dimensions; never infer package dimensions from this label."),
    ),
}


def _category_id(category: str | CategoryResult) -> str:
    category_id = category.category_id if isinstance(category, CategoryResult) else category
    if category_id not in CATEGORY_NAMES:
        raise ValueError(f"unsupported category: {category_id}")
    return category_id


def get_attribute_schema(
    category: str | CategoryResult,
    *,
    include_universal: bool = True,
) -> list[AttributeDefinition]:
    """Return universal plus category definitions without using them as a whitelist."""
    category_id = _category_id(category)
    definitions = list(UNIVERSAL_ATTRIBUTES if include_universal else ())
    definitions.extend(CATEGORY_ATTRIBUTES.get(category_id, ()))
    merged: dict[str, AttributeDefinition] = {}
    for definition in definitions:
        merged[definition.canonical_name] = definition
    return list(merged.values())


def get_expected_attributes(category: str | CategoryResult) -> list[AttributeDefinition]:
    """Future Gap Analysis contract: definitions expected for this category."""
    return [definition for definition in get_attribute_schema(category) if definition.expected]


def _alias_key(value: str) -> str:
    text = unicodedata.normalize("NFKC", value).casefold()
    return " ".join(re.findall(r"[\w]+", text))


def resolve_attribute_definition(
    name: str,
    category: str | CategoryResult,
) -> AttributeDefinition | None:
    """Resolve only unambiguous aliases within the selected layered schema."""
    key = _alias_key(name)
    matches: list[AttributeDefinition] = []
    for definition in get_attribute_schema(category):
        keys = {_alias_key(definition.canonical_name), *map(_alias_key, definition.aliases)}
        if key in keys:
            matches.append(definition)
    unique = {item.canonical_name: item for item in matches}
    return next(iter(unique.values())) if len(unique) == 1 else None


def analyze_schema_coverage(
    category: str | CategoryResult,
    attributes: Iterable[AttributeLike | str] | Any,
) -> SchemaDiagnostics:
    """Diagnose coverage while preserving every unknown valid extracted fact."""
    mapping_result = all(
        hasattr(attributes, name) for name in ("mapped", "derived", "unmapped", "ambiguous")
    )
    items = [*attributes.mapped, *attributes.derived] if mapping_result else list(attributes)
    present: dict[str, list[object]] = {}
    discovered: list[object] = (
        [*attributes.unmapped, *(item.raw_attribute for item in attributes.ambiguous)]
        if mapping_result else []
    )
    for item in items:
        name = item if isinstance(item, str) else getattr(item, "canonical_name", None) or item.name
        definition = next(
            (candidate for candidate in get_attribute_schema(category)
             if candidate.canonical_name == name),
            None,
        ) or resolve_attribute_definition(name, category)
        if definition is None:
            discovered.append(item)
            continue
        present.setdefault(definition.canonical_name, []).append(item)
    missing = [
        definition.canonical_name
        for definition in get_expected_attributes(category)
        if definition.canonical_name not in present
    ]
    return SchemaDiagnostics(present, missing, discovered)


def extend_schema_with_discovered(
    category: str | CategoryResult,
    attributes: Iterable[AttributeLike | str],
) -> list[AttributeDefinition]:
    """Add unknown raw fields as non-expected discovered definitions, never discard them."""
    definitions = get_attribute_schema(category)
    seen = {definition.canonical_name for definition in definitions}
    for item in attributes:
        name = item if isinstance(item, str) else item.name
        if resolve_attribute_definition(name, category) is not None:
            continue
        canonical_name = _alias_key(name).replace(" ", "_") or "unnamed_discovered_attribute"
        if canonical_name in seen:
            continue
        seen.add(canonical_name)
        definitions.append(_definition(
            canonical_name,
            (name,),
            scope="discovered",
            value_type="unknown",
            priority="low",
            expected=False,
            notes="Preserved from extraction; semantics are not predefined.",
        ))
    return definitions
