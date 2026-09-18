"""Extract raw, source-faithful product attributes from a FetchResult."""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Any, Iterable

from bs4 import BeautifulSoup, Tag

METHOD_PRIORITY = {
    "json_ld": 10,
    "embedded_json": 9,
    "structured_data": 8,
    "html_table": 7,
    "definition_list": 6,
    "label_value": 5,
    "pdf_spec": 4,
    "spec_block": 3,
    "pdf_text": 2,
    "plain_text": 1,
}
IDENTITY_FIELD_PATTERN = re.compile(
    r"^(?:sku|mpn|gtin\d*|ean(?:\s*code)?|upc|article(?:\s*(?:no|number|code))?|"
    r"model(?:\s*(?:no|number|code))?|brand|manufacturer|product\s*(?:id|code)|"
    r"артикул|модель|бренд|производитель|код\s+товара|"
    r"ბრენდი|მწარმოებელი|პროდუქტის\s+კოდი)$",
    re.IGNORECASE,
)
COMMERCE_FIELD_PATTERN = re.compile(
    r"\b(?:price|currency|availability|item\s*condition|old\s*price|stock|rating|"
    r"reviews?|views?|delivery|shipping)\b|"
    r"\b(?:цена|валюта|наличие|наличии|доставка|рейтинг|отзывы?|просмотры?|"
    r"стоимость|розничные\s+предложения)\b|"
    r"(?:ფასი|ძველი\s+ფასი|ნანახია|მიწოდება)",
    re.IGNORECASE,
)
PRODUCT_FIELD_PATTERN = re.compile(
    r"\b(?:power|weight|dimensions?|width|height|depth|length|size|capacity|volume|"
    r"voltage|frequency|material|colou?r|type|control|timer|display|resolution|"
    r"memory|camera|battery|charging|runtime|temperature|programs?|zones?|"
    r"wlan|wi-?fi|bluetooth|nfc|positioning|accessories|contents?)\b|"
    r"\b(?:мощность|вес|габариты|размеры|ширина|высота|глубина|длина|объ[её]м|"
    r"напряжение|частота|материал|цвет|тип|управление|таймер|температура|"
    r"программы?|комплектация)\b",
    re.IGNORECASE,
)
UNIT_TOKEN = (
    r"mm|cm|km|m|mg|kg|g|lb|oz|ml|cl|l|mAh|mA·h|Ah|kPa|Pa|kW|W|V|A|Hz|MHz|GHz|GB|TB|MB|nit|nits|°C|°F|%|rpm|dB|"
    r"мм|см|км|мг|кг|г|мл|кл|л|мАч|мА·год|Ач|А·год|кПа|Па|кВт|Вт|В|Гц|кГц|МГц|мин|хв|год|об/мин|дБ"
)
UNIT_PATTERN = re.compile(
    rf"^\s*([-+]?\d+(?:[.,]\d+)?(?:\s*(?:[x×;]|[-–—])\s*[-+]?\d+(?:[.,]\d+)?)*)\s*({UNIT_TOKEN})\s*$",
    re.IGNORECASE,
)
PAIR_PATTERN = re.compile(r"^(.{1,120}?)(?:\s*:\s*|\s+\.{2,}\s*)(.{1,500})$")
NUMERIC_PAIR_PATTERN = re.compile(
    r"^([^:]{1,100}?[A-Za-zА-Яа-я])\s+([-+]?\d+(?:[.,]\d+)?(?:\s*[x×]\s*\d+(?:[.,]\d+)?){0,3}(?:\s*[A-Za-z°%]+)?)$"
)
# A bare 1-5 star rating immediately followed by a date line is a review
# widget's "reviewer name / rating / date" triple, not a spec label/value
# pair - observed live on a manufacturer page (a reviewer's name paired
# with their star rating). Generic across any review widget, not tied to
# a specific site's markup.
REVIEW_RATING_LINE_PATTERN = re.compile(r"^[1-5](?:[.,]0)?$")
REVIEW_DATE_LINE_PATTERN = re.compile(r"^\d{1,2}[./]\d{1,2}[./]\d{2,4}$")
FACTUAL_JSON_FIELDS = ("sku", "mpn", "gtin", "gtin8", "gtin12", "gtin13", "gtin14",
                       "productID", "model", "brand", "color", "material", "weight",
                       "width", "height", "depth", "size", "category")
OFFER_FIELDS = ("price", "priceCurrency", "availability", "itemCondition")
PRODUCT_STATE_KEYS = re.compile(
    r"^(?:product|productdata|productdetail|productdetails|productinfo|pdp)$",
    re.IGNORECASE,
)
SPECIFICATION_KEYS = re.compile(
    r"^(?:spec|specs|specification|specifications|technicalspecifications|"
    r"attributes|properties|features)$",
    re.IGNORECASE,
)
STATE_SCRIPT_ID_PATTERN = re.compile(
    r"^(?:__NEXT_DATA__|__NUXT_DATA__|__INITIAL_STATE__|__APOLLO_STATE__)$",
    re.IGNORECASE,
)
STATE_ASSIGNMENT_PATTERN = re.compile(
    r"^(?:(?:window\.)?(?:__NUXT__|__INITIAL_STATE__|__APOLLO_STATE__)\s*=\s*)"
    r"(?P<payload>[\[{].*[\]}])\s*;?\s*$",
    re.DOTALL,
)
STRUCTURED_SPEC_REGION_PATTERN = re.compile(
    r"spec|feature|attribute|property|detail|parameter|characteristic",
    re.IGNORECASE,
)
SKIP_LABELS = {
    "home", "menu", "more", "next", "previous", "read more", "share", "search",
    "password", "compare", "payment options", "comments / reviews", "review this product",
    "add to wish list", "cart items", "rates", "sign in", "tel",
}
UI_CONTAINER_NAMES = {"nav", "header", "footer", "aside"}
UI_CONTAINER_PATTERN = re.compile(
    r"menu|breadcrumb|navigation|cart|checkout|delivery|shipping|payment|"
    r"recommendation|related|popular|footer|header",
    re.IGNORECASE,
)
UI_LABEL_PATTERN = re.compile(
    r"^(?:add|buy|choose|click|continue|enter|fill|log|order|proceed|read|"
    r"select|sign|subscribe|view)\b|^what we (?:do|offer|provide)\b|"
    r"^(?:most popular|related|recommended)\b",
    re.IGNORECASE,
)
UI_VALUE_PATTERN = re.compile(
    r"\b(?:working days?|business days?|get it|checkout|payment)\b",
    re.IGNORECASE,
)
SAFE_PDF_CONTINUATION = re.compile(
    rf"^[-+]?\d[\d\s.,;:+/x×%\-–—]*(?:{UNIT_TOKEN})?[)\]]+$",
    re.IGNORECASE,
)
FEATURE_GROUP_LABEL_PATTERN = re.compile(
    r"^(?:features?|specifications?|характеристики|მახასიათებლები)$",
    re.IGNORECASE,
)
DRYING_TERM_PATTERN = re.compile(
    r"\bdry(?:ing)?\b|сушк\w*|გაშრობ\w*",
    re.IGNORECASE,
)
TEMPERATURE_VALUE_PATTERN = re.compile(r"[-+]?\d+(?:[.,]\d+)?\s*°\s*[CF]", re.IGNORECASE)
PDF_PAIR_PATTERN = re.compile(r"^(.{1,120}?)\s*:\s*(.{1,300})$")
PDF_UNIT_TERMINATED_PATTERN = re.compile(
    rf"^([^:\d]{{1,60}}?)\s+([-+]?\d+(?:[.,]\d+)?\s*(?:{UNIT_TOKEN}))\.?$"
)
PDF_BULLET_PREFIX_PATTERN = re.compile(r"^[•▪◦‣·*\-–—]\s+")
PDF_TOC_DOT_LEADER_PATTERN = re.compile(r"\.{4,}")
PDF_PAGE_NUMBER_LINE_PATTERN = re.compile(r"^\d{1,3}$")
PDF_LANGUAGE_MARKER_LINE_PATTERN = re.compile(r"^[A-Z]{2}$")
PDF_SPEC_HEADING_PATTERN = re.compile(
    r"specifications?|technical\s+data|technical\s+characteristics?|parameters?|"
    r"характеристик|параметр|спецификац",
    re.IGNORECASE,
)
PDF_CONTACT_LABEL_PATTERN = re.compile(
    r"телефон|горяч(?:ей|ая)\s+лини|служба\s+поддержк|эл\.?\s*почта|"
    r"hotline|customer\s+service|support\s+line|phone\s*(?:number)?|e-?mail",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class RawAttribute:
    name: str
    value: str
    unit: str | None
    source_url: str
    source_type: str | None
    evidence: str
    extraction_method: str
    confidence: str
    raw_value: str
    attribute_kind: str
    context: str | None = None
    # Stage 31.5: stated for the exact model column of an official spec table
    # without a variant qualifier, so it holds for every variant of the model.
    model_wide: bool = False
    # Stage 32: the canonical attribute an official-table reader already decided
    # on (language-neutral identifier); honoured by mapping before any alias lookup.
    canonical: str | None = None


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def split_value_unit(raw_value: str) -> tuple[str, str | None]:
    """Split only an unambiguous numeric value and unit; never convert it."""
    raw = _clean(raw_value)
    match = UNIT_PATTERN.fullmatch(raw)
    return (match.group(1), match.group(2)) if match else (raw, None)


def _plausible_pair(name: str, value: str, *, generic: bool = False) -> bool:
    name, value = _clean(name), _clean(value)
    if not name or not value or name.casefold() == value.casefold():
        return False
    if len(name) > 120 or len(value) > 500 or len(name.split()) > 14:
        return False
    low = name.casefold().strip(":")
    if low in SKIP_LABELS or "http://" in low or "https://" in low:
        return False
    if not any(character.isalpha() for character in name):
        return False
    if name.lstrip().startswith(("-", "•", "�")) and not re.match(r"^[-+]?\d", value):
        return False
    if any(term in low for term in (
        "forgot your password", "order process", "delivery in", "write a review", "installment",
    )):
        return False
    if re.search(r"(?:,|\b(?:and|or|the|to|with))$", value.casefold()):
        return False
    if name.count(".") > 2 and not re.search(r"\.{3,}$", name):
        return False
    if not generic:
        return True
    if len(name) > 100 or len(name.split()) > 16:
        return False
    if name.rstrip().endswith((".", "!", "?")) or len(re.findall(r"[.!?](?:\s|$)", name)) > 1:
        return False
    if UI_LABEL_PATTERN.search(name) or UI_VALUE_PATTERN.search(value):
        return False
    alphanumeric = [character for character in name if character.isalnum()]
    if not alphanumeric or sum(character.isalpha() for character in alphanumeric) / len(alphanumeric) < 0.3:
        return False
    return not (value.endswith(":") and len(value.split()) > 1)


def _evidence(name: str, raw_value: str, prefix: str = "") -> str:
    text = f"{prefix}{_clean(name)} = {_clean(raw_value)}" if prefix else f"{_clean(name)} | {_clean(raw_value)}"
    return text[:300]


def _attribute_kind(name: str, raw_value: str, method: str,
                    explicit_kind: str | None = None) -> str:
    if explicit_kind:
        return explicit_kind
    if IDENTITY_FIELD_PATTERN.search(name):
        return "identity"
    if COMMERCE_FIELD_PATTERN.search(name):
        return "commerce"
    if re.fullmatch(r"[A-Z]{3}", name) and re.fullmatch(r"\d+(?:[.,]\d+)?", raw_value):
        return "commerce"
    if PRODUCT_FIELD_PATTERN.search(name) or split_value_unit(raw_value)[1] is not None:
        return "product"
    if method in {"json_ld", "html_table", "label_value", "pdf_text", "pdf_spec"}:
        return "product"
    return "unknown"


def _attribute(name: Any, raw_value: Any, source_url: str, source_type: str | None,
               method: str, confidence: str, evidence: str | None = None,
               explicit_unit: str | None = None, *, generic: bool = False,
               attribute_kind: str | None = None,
               context: str | None = None) -> RawAttribute | None:
    name, raw_value = _clean(name), _clean(raw_value)
    raw_value = re.sub(r"^\.{2,}\s*", "", raw_value)
    if not _plausible_pair(name, raw_value, generic=generic):
        return None
    value, unit = split_value_unit(raw_value)
    if explicit_unit:
        unit = _clean(explicit_unit)
    return RawAttribute(name, value, unit, source_url, source_type,
                        _clean(evidence)[:300] if evidence else _evidence(name, raw_value),
                        method, confidence, raw_value,
                        _attribute_kind(name, raw_value, method, attribute_kind),
                        _clean(context)[:300] or None)


CONTEXT_HEADING_PATTERN = re.compile(
    r"(?:^|[-_\s])(title|heading|header|section-name|group-name)(?=$|[-_\s])",
    re.I,
)


def _context_for_tag(tag: Tag, label: str | None = None) -> str | None:
    """Collect nearby DOM section signals without inventing semantic context."""
    values: list[str] = []
    label_key = _clean(label).casefold()
    current: Tag | None = tag
    for _ in range(6):
        if not isinstance(current, Tag):
            break
        for key in ("aria-label", "data-section", "data-group"):
            value = _clean(current.get(key))
            if value and value.casefold() != label_key:
                values.append(value)
        for heading in current.find_all(
            lambda node: isinstance(node, Tag) and (
                node.name in {"h1", "h2", "h3", "h4", "h5", "h6", "legend", "caption"}
                or CONTEXT_HEADING_PATTERN.search(" ".join(node.get("class", [])))
            ),
            recursive=False,
            limit=2,
        ):
            text = _clean(heading.get_text(" ", strip=True))
            if text and len(text) <= 120 and text.casefold() != label_key:
                values.append(text)
        for sibling in list(current.previous_siblings)[:3]:
            if not isinstance(sibling, Tag):
                continue
            signals = " ".join(sibling.get("class", []))
            if sibling.name not in {"h1", "h2", "h3", "h4", "h5", "h6", "legend"} \
                    and not CONTEXT_HEADING_PATTERN.search(signals):
                continue
            text = _clean(sibling.get_text(" ", strip=True))
            if text and len(text) <= 120 and text.casefold() != label_key:
                values.append(text)
        current = current.parent if isinstance(current.parent, Tag) else None
    unique = list(dict.fromkeys(value for value in values if value))
    return " | ".join(unique)[:300] or None


def _types(node: dict[str, Any]) -> set[str]:
    value = node.get("@type", [])
    values = value if isinstance(value, list) else [value]
    return {_clean(item).casefold() for item in values}


def _json_value(value: Any) -> tuple[str, str | None]:
    if isinstance(value, dict):
        unit = value.get("unitText") or value.get("unitCode")
        raw = value.get("value") or value.get("name") or value.get("@value")
        return _clean(raw), _clean(unit) or None
    if isinstance(value, list):
        return ", ".join(filter(None, (_clean(item) for item in value))), None
    return _clean(value), None


def _walk_json(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_json(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_json(child)


def _extract_json_ld(soup: BeautifulSoup, source_url: str, source_type: str | None) -> list[RawAttribute]:
    found: list[RawAttribute] = []
    for script in soup.find_all("script", attrs={"type": re.compile(r"application/ld\+json", re.I)}):
        try:
            payload = json.loads(script.string or script.get_text() or "")
        except (json.JSONDecodeError, TypeError):
            continue
        for node in _walk_json(payload):
            node_types = _types(node)
            if "product" in node_types:
                for field in FACTUAL_JSON_FIELDS:
                    if field in node:
                        raw, unit = _json_value(node[field])
                        item = _attribute(field, raw, source_url, source_type, "json_ld", "high",
                                          _evidence(field, raw, "Product: "), unit,
                                          attribute_kind="identity" if IDENTITY_FIELD_PATTERN.search(field) else "product")
                        if item:
                            found.append(item)
                properties = node.get("additionalProperty", [])
                properties = properties if isinstance(properties, list) else [properties]
                for prop in properties:
                    if not isinstance(prop, dict) or "propertyvalue" not in _types(prop):
                        continue
                    name = prop.get("name") or prop.get("propertyID")
                    raw, unit = _json_value(prop)
                    item = _attribute(name, raw, source_url, source_type, "json_ld", "high",
                                      _evidence(name, raw, "additionalProperty: "), unit)
                    if item:
                        found.append(item)
            elif "offer" in node_types:
                for field in OFFER_FIELDS:
                    if field in node:
                        raw, unit = _json_value(node[field])
                        item = _attribute(field, raw, source_url, source_type, "json_ld", "high",
                                          _evidence(field, raw, "Offer: "), unit,
                                          attribute_kind="commerce")
                        if item:
                            found.append(item)
    return found


def _state_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", _clean(value).casefold())


def _state_scalar(value: Any) -> str | None:
    if value is None or isinstance(value, (dict, list)):
        return None
    text = _clean(str(value))
    return text or None


def _walk_json_paths(
    value: Any,
    path: tuple[str, ...] = (),
    depth: int = 0,
) -> Iterable[tuple[tuple[str, ...], dict[str, Any]]]:
    if depth > 12:
        return
    if isinstance(value, dict):
        yield path, value
        for key, child in value.items():
            yield from _walk_json_paths(child, (*path, _clean(key)), depth + 1)
    elif isinstance(value, list):
        for child in value[:2000]:
            yield from _walk_json_paths(child, path, depth + 1)


def _dict_value(node: dict[str, Any], *names: str) -> Any:
    wanted = {_state_key(name) for name in names}
    for key, value in node.items():
        if _state_key(key) in wanted:
            return value
    return None


def _spec_pair(node: dict[str, Any]) -> tuple[str, str, str | None] | None:
    name = _dict_value(node, "name", "label", "key", "propertyName", "propertyID")
    raw_value = _dict_value(node, "value", "displayValue", "text")
    name_text = _state_scalar(name)
    if not name_text:
        return None
    if isinstance(raw_value, dict):
        raw, unit = _json_value(raw_value)
        return (name_text, raw, unit) if raw else None
    if isinstance(raw_value, list):
        scalar_values = [_state_scalar(item) for item in raw_value]
        if not scalar_values or any(item is None for item in scalar_values):
            return None
        return name_text, ", ".join(item for item in scalar_values if item), None
    raw = _state_scalar(raw_value)
    unit = _state_scalar(_dict_value(node, "unit", "unitText", "unitCode"))
    return (name_text, raw, unit) if raw else None


def _flatten_spec_state(
    value: Any,
    source_url: str,
    source_type: str | None,
    *,
    method: str,
    context: tuple[str, ...] = (),
    depth: int = 0,
) -> list[RawAttribute]:
    if depth > 10:
        return []
    found: list[RawAttribute] = []
    if isinstance(value, list):
        for child in value[:1000]:
            found.extend(_flatten_spec_state(
                child, source_url, source_type, method=method,
                context=context, depth=depth + 1,
            ))
        return found
    if not isinstance(value, dict):
        return found

    pair = _spec_pair(value)
    if pair:
        name, raw, unit = pair
        context_text = " | ".join(context) or None
        item = _attribute(
            name, raw, source_url, source_type, method, "high",
            evidence=_evidence(name, raw, f"{method}: "),
            explicit_unit=unit, generic=True, attribute_kind="product",
            context=context_text,
        )
        return [item] if item else []

    child_keys = {
        "items", "values", "children", "groups", "sections", "entries",
        "specs", "specifications", "attributes", "properties", "features",
    }
    has_children = any(_state_key(key) in child_keys for key in value)
    group = _state_scalar(_dict_value(value, "group", "section", "title", "category"))
    if group is None and has_children:
        group = _state_scalar(_dict_value(value, "name", "label"))
    next_context = (*context, group) if group and group not in context else context
    metadata_keys = {
        "id", "type", "typename", "icon", "image", "url", "href", "slug",
        "sort", "sortorder", "order", "description", "disclaimer", "footnote",
        "name", "label", "group", "section", "title", "category",
    }
    for key, child in value.items():
        normalized = _state_key(key)
        if normalized in metadata_keys:
            continue
        scalar = _state_scalar(child)
        if scalar is not None:
            item = _attribute(
                key, scalar, source_url, source_type, method, "high",
                evidence=_evidence(key, scalar, f"{method}: "),
                generic=True, attribute_kind="product",
                context=" | ".join(next_context) or None,
            )
            if item:
                found.append(item)
            continue
        child_context = next_context
        if normalized not in child_keys and not re.fullmatch(r"\d+", normalized):
            child_context = (*next_context, _clean(key))
        found.extend(_flatten_spec_state(
            child, source_url, source_type, method=method,
            context=child_context, depth=depth + 1,
        ))
    return found


def _product_state_attributes(
    payload: Any,
    source_url: str,
    source_type: str | None,
) -> list[RawAttribute]:
    found: list[RawAttribute] = []
    visited: set[int] = set()
    identity_fields = {
        "brand": "brand",
        "manufacturer": "manufacturer",
        "model": "model",
        "sku": "sku",
        "mpn": "mpn",
        "gtin": "gtin",
        "gtin8": "gtin8",
        "gtin12": "gtin12",
        "gtin13": "gtin13",
        "gtin14": "gtin14",
        "productid": "productID",
        "producttype": "category",
        "category": "category",
        "color": "color",
    }
    for path, node in _walk_json_paths(payload):
        path_key = _state_key(path[-1]) if path else ""
        explicit_product = "product" in _types(node)
        product_scope = explicit_product or bool(PRODUCT_STATE_KEYS.fullmatch(path_key))
        spec_keys = [key for key in node if SPECIFICATION_KEYS.fullmatch(_state_key(key))]
        if not product_scope or (not explicit_product and not spec_keys):
            continue
        if id(node) in visited:
            continue
        visited.add(id(node))

        for key, value in node.items():
            label = identity_fields.get(_state_key(key))
            if label is None:
                continue
            raw, unit = _json_value(value)
            item = _attribute(
                label, raw, source_url, source_type, "embedded_json", "high",
                evidence=_evidence(label, raw, "product_state: "),
                explicit_unit=unit,
                attribute_kind=(
                    "identity" if IDENTITY_FIELD_PATTERN.search(label) else "product"
                ),
            )
            if item:
                found.append(item)
        for key in spec_keys:
            found.extend(_flatten_spec_state(
                node[key], source_url, source_type,
                method="embedded_json",
            ))
    return found


def _script_json_payload(script: Tag) -> Any | None:
    script_type = _clean(script.get("type")).casefold()
    if script_type == "application/ld+json":
        return None
    raw = script.string or script.get_text() or ""
    if not raw or len(raw) > 2_000_000:
        return None
    stripped = raw.strip()
    script_id = _clean(script.get("id"))
    if stripped.startswith(("{", "[")) and (
        script_type in {"application/json", "application/state+json"}
        or STATE_SCRIPT_ID_PATTERN.fullmatch(script_id)
        or not script_type
    ):
        candidate = stripped
    else:
        match = STATE_ASSIGNMENT_PATTERN.fullmatch(stripped)
        if not match:
            return None
        candidate = match.group("payload")
    try:
        return json.loads(candidate)
    except (json.JSONDecodeError, TypeError, RecursionError):
        return None


def _extract_embedded_product_state(
    soup: BeautifulSoup,
    source_url: str,
    source_type: str | None,
) -> list[RawAttribute]:
    found: list[RawAttribute] = []
    for script in soup.find_all("script"):
        payload = _script_json_payload(script)
        if payload is not None:
            found.extend(_product_state_attributes(payload, source_url, source_type))
    for tag in soup.find_all(True):
        for attr_name, raw in tag.attrs.items():
            if not attr_name.startswith("data-") or not re.search(
                r"product|spec|attribute|feature", attr_name, re.IGNORECASE,
            ):
                continue
            if not isinstance(raw, str) or not raw.lstrip().startswith(("{", "[")):
                continue
            try:
                payload = json.loads(raw)
            except (json.JSONDecodeError, TypeError, RecursionError):
                continue
            if re.search(r"spec|attribute|feature", attr_name, re.IGNORECASE):
                found.extend(_flatten_spec_state(
                    payload, source_url, source_type, method="embedded_json",
                ))
            else:
                found.extend(_product_state_attributes(
                    {"product": payload}, source_url, source_type,
                ))
    return found


def _extract_structured_data_values(
    soup: BeautifulSoup,
    source_url: str,
    source_type: str | None,
) -> list[RawAttribute]:
    """Extract labelled data-value facts only inside proven specification rows."""
    found: list[RawAttribute] = []
    for value_node in soup.find_all(attrs={"data-value": True}):
        raw = _clean(value_node.get("data-value"))
        data_class = _clean(value_node.get("data-class")).casefold()
        if not raw or data_class in {"p4", "note", "footnote", "disclaimer"}:
            continue
        container: Tag | None = value_node
        label_node: Tag | None = None
        for _ in range(7):
            if not isinstance(container, Tag):
                break
            classes = container.get("class") or []
            if isinstance(classes, str):
                classes = [classes]
            signals = " ".join([
                _clean(container.get("id")),
                *(_clean(value) for value in classes),
                _clean(container.get("data-section")),
                _clean(container.get("data-group")),
            ])
            candidate_label = container.find(
                ["h2", "h3", "h4", "dt", "legend"], recursive=True,
            )
            if STRUCTURED_SPEC_REGION_PATTERN.search(signals) and candidate_label:
                label_node = candidate_label
                break
            container = container.parent if isinstance(container.parent, Tag) else None
        if container is None or label_node is None or _inside_ui_region(container):
            continue
        name = _clean(label_node.get_text(" ", strip=True))
        item = _attribute(
            name, raw, source_url, source_type, "structured_data", "high",
            evidence=_evidence(name, raw, "data-value: "),
            generic=True, attribute_kind="product",
            context=_context_for_tag(container, name),
        )
        if item:
            found.append(item)
    return found


def _extract_tables(soup: BeautifulSoup, source_url: str, source_type: str | None) -> list[RawAttribute]:
    found = []
    for row in soup.select("table tr"):
        if _inside_ui_region(row):
            continue
        cells = row.find_all(["th", "td"], recursive=False)
        if len(cells) != 2:
            continue
        name, raw = (_clean(cell.get_text(" ", strip=True)) for cell in cells)
        if cells[0].find_all("a") and len(name) > 80:
            continue
        item = _attribute(name, raw, source_url, source_type, "html_table", "high",
                          context=_context_for_tag(row, name))
        if item:
            found.append(item)
    return found


def _extract_definitions(soup: BeautifulSoup, source_url: str, source_type: str | None) -> list[RawAttribute]:
    found = []
    for term in soup.find_all("dt"):
        if _inside_ui_region(term):
            continue
        value = term.find_next_sibling("dd")
        if value:
            item = _attribute(term.get_text(" ", strip=True), value.get_text(" ", strip=True),
                              source_url, source_type, "definition_list", "high",
                              context=_context_for_tag(term, term.get_text(" ", strip=True)))
            if item:
                found.append(item)
    return found


def _extract_label_values(soup: BeautifulSoup, source_url: str, source_type: str | None) -> list[RawAttribute]:
    found = []
    label_re = re.compile(r"(?:^|[-_])(label|name|key)(?:$|[-_])", re.I)
    value_re = re.compile(r"(?:^|[-_])(value|val|data|text)(?:$|[-_])", re.I)
    container_re = re.compile(r"spec|feature|attribute|property|detail|parameter|characteristic", re.I)
    for label in soup.find_all(class_=label_re):
        if _inside_ui_region(label):
            continue
        parent = label.parent if isinstance(label.parent, Tag) else None
        label_classes = " ".join(label.get("class", []))
        parent_classes = " ".join(parent.get("class", [])) if parent else ""
        if not container_re.search(f"{label_classes} {parent_classes}"):
            continue
        value = parent.find(class_=value_re) if parent else None
        if value is None:
            value = label.find_next_sibling()
        if value is not None:
            item = _attribute(label.get_text(" ", strip=True), value.get_text(" ", strip=True),
                              source_url, source_type, "label_value", "medium", generic=True,
                              context=_context_for_tag(parent or label, label.get_text(" ", strip=True)))
            if item:
                found.append(item)
    return found


def _direct_element_children(tag: Tag) -> list[Tag]:
    return [child for child in tag.children if isinstance(child, Tag)]


def _row_pair(row: Tag) -> tuple[str, str] | None:
    children = _direct_element_children(row)
    if len(children) != 2:
        return None
    name = _clean(children[0].get_text(" ", strip=True))
    value = _clean(children[1].get_text(" ", strip=True))
    if not _plausible_pair(name, value, generic=True):
        return None
    return name, value


def _inline_label_pair(row: Tag) -> tuple[str, str] | None:
    """Read a direct bold/strong label followed by text in the same row."""
    children = _direct_element_children(row)
    if not children or children[0].name not in {"b", "strong"}:
        return None
    raw_label = _clean(children[0].get_text(" ", strip=True))
    full_text = _clean(row.get_text(" ", strip=True))
    if not raw_label or not full_text.startswith(raw_label):
        return None
    value = full_text[len(raw_label):].lstrip(" :–—-")
    label = raw_label.rstrip(":").strip()
    if not label or not value or len(label) > 120:
        return None
    return label, value


def _proven_inline_label_rows(soup: BeautifulSoup) -> list[tuple[Tag, tuple[str, str]]]:
    """Return inline label rows only when siblings prove a repeated fact structure."""
    found: list[tuple[Tag, tuple[str, str]]] = []
    consumed: set[int] = set()
    for container in soup.find_all(["div", "section", "ul", "ol"]):
        if _inside_ui_region(container) or _high_link_density(container):
            continue
        rows = [
            row for row in _direct_element_children(container)
            if row.name in {"p", "li", "div"}
        ]
        if len(rows) < 3:
            continue
        valid = [(row, _inline_label_pair(row)) for row in rows]
        pairs = [(row, pair) for row, pair in valid if pair is not None]
        if len(pairs) < 3 or len(pairs) / len(rows) < 0.4:
            continue
        for row, pair in pairs:
            if id(row) not in consumed:
                consumed.add(id(row))
                found.append((row, pair))
    return found


def _extract_feature_temperatures(
    row: Tag,
    group_value: str,
    source_url: str,
    source_type: str | None,
) -> list[RawAttribute]:
    """Extract an explicit drying temperature inside a proven Features row."""
    found: list[RawAttribute] = []
    for drying in DRYING_TERM_PATTERN.finditer(group_value):
        clause = re.split(r"[.!?;]", group_value[drying.start():], maxsplit=1)[0]
        temperatures = list(TEMPERATURE_VALUE_PATTERN.finditer(clause))
        if not temperatures:
            continue
        temperature = temperatures[-1]
        source_label = drying.group(0)
        item = _attribute(
            source_label,
            temperature.group(0),
            source_url,
            source_type,
            "label_value",
            "medium",
            evidence=_clean(clause)[:300],
            generic=True,
            context=_context_for_tag(row, source_label),
        )
        if item:
            found.append(item)
    return found


FEATURE_MEASUREMENT_PATTERNS = (
    (
        re.compile(
            r"(?P<value>\d+(?:[.,]\d+)?)\s*(?P<unit>hz)\b.{0,30}?\brefresh\s+rate\b",
            re.IGNORECASE,
        ),
        "Refresh Rate",
    ),
    (
        re.compile(
            r"(?P<value>\d+(?:[.,]\d+)?)\s*(?P<unit>nits?)\b.{0,30}?\bpeak\s+brightness\b",
            re.IGNORECASE,
        ),
        "Peak Brightness",
    ),
    (
        re.compile(
            r"(?P<value>\d+(?:[.,]\d+)?)\s*(?P<unit>mah|ma·h|мАч|мА·год)\b"
            r".{0,30}?\b(?:battery|аккумулятор|акумулятор)\b",
            re.IGNORECASE,
        ),
        "Battery Capacity",
    ),
)


def _extract_labeled_feature_measurements(
    name: str,
    value: str,
    source_url: str,
    source_type: str | None,
    *,
    extraction_method: str,
    confidence: str,
    evidence: str,
    context: str | None,
) -> list[RawAttribute]:
    """Recover explicit measurement + semantic-label feature-card facts."""
    text = _clean(f"{name} {value}")
    found: list[RawAttribute] = []
    for pattern, canonical_label in FEATURE_MEASUREMENT_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        raw_value = f"{match.group('value')} {match.group('unit')}"
        item = _attribute(
            canonical_label,
            raw_value,
            source_url,
            source_type,
            extraction_method,
            confidence,
            evidence=evidence or text[:300],
            generic=True,
            context=context,
        )
        if item:
            found.append(item)
    return found


def _extract_inline_label_blocks(
    soup: BeautifulSoup,
    source_url: str,
    source_type: str | None,
) -> list[RawAttribute]:
    found: list[RawAttribute] = []
    for row, (name, value) in _proven_inline_label_rows(soup):
        item = _attribute(
            name, value, source_url, source_type, "label_value", "medium",
            generic=True, context=_context_for_tag(row, name),
        )
        if item:
            found.append(item)
        if FEATURE_GROUP_LABEL_PATTERN.fullmatch(name):
            found.extend(_extract_feature_temperatures(row, value, source_url, source_type))
    return found


def _extract_repeated_blocks(soup: BeautifulSoup, source_url: str,
                             source_type: str | None) -> list[RawAttribute]:
    """Extract repeated two-column/block pairs only inside a proven sequence."""
    found: list[RawAttribute] = []
    consumed: set[int] = set()
    for container in soup.find_all(["div", "section", "ul", "ol"]):
        if _inside_ui_region(container) or _high_link_density(container):
            continue
        rows = _direct_element_children(container)
        if len(rows) < 3:
            continue
        pairs = [(row, _row_pair(row)) for row in rows]
        valid = [(row, pair) for row, pair in pairs if pair is not None]
        signals = " ".join(
            " ".join([_clean(node.get("id")), *(_clean(value) for value in node.get("class", []))])
            for node in [container, *list(container.parents)[:2]] if isinstance(node, Tag)
        )
        proven_spec_region = bool(re.search(
            r"spec|feature|attribute|property|detail|parameter|characteristic", signals, re.I
        ))
        signatures = {
            (row.name, tuple(row.get("class", []))) for row, _pair in valid
        }
        minimum = 2 if proven_spec_region and len(signatures) == 1 else 3
        if len(valid) < minimum or len(valid) / len(rows) < 0.5:
            continue
        for row, pair in valid:
            marker = id(row)
            if marker in consumed:
                continue
            consumed.add(marker)
            name, value = pair
            item = _attribute(name, value, source_url, source_type,
                              "label_value", "medium", generic=True,
                              context=_context_for_tag(row, name))
            if item:
                found.append(item)
    return found


def _walk_json_limited(value: Any, depth: int = 0) -> Iterable[str]:
    if depth > 12:
        return
    if isinstance(value, str):
        if len(value) <= 250_000 and re.search(r"<(?:table|dl|div|section|ul)\b", value, re.I):
            yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from _walk_json_limited(child, depth + 1)
    elif isinstance(value, list):
        for child in value[:2000]:
            yield from _walk_json_limited(child, depth + 1)


def _extract_embedded_structures(soup: BeautifulSoup, source_url: str,
                                 source_type: str | None) -> list[RawAttribute]:
    found: list[RawAttribute] = []
    fragments_seen: set[str] = set()
    fragment_count = 0
    for script in soup.find_all("script"):
        script_type = _clean(script.get("type")).casefold()
        raw = script.string or script.get_text() or ""
        stripped = raw.lstrip()
        if script_type == "application/ld+json":
            continue
        if script_type not in {"application/json", "application/state+json"} and not stripped.startswith(("{", "[")):
            continue
        if not stripped or len(raw) > 2_000_000:
            continue
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, TypeError, RecursionError):
            continue
        for html in _walk_json_limited(payload):
            key = html[:1000]
            if key in fragments_seen:
                continue
            fragments_seen.add(key)
            fragment_count += 1
            if fragment_count > 100:
                return found
            fragment = BeautifulSoup(html, "html.parser")
            found.extend(_extract_tables(fragment, source_url, source_type))
            found.extend(_extract_definitions(fragment, source_url, source_type))
            found.extend(_extract_label_values(fragment, source_url, source_type))
            found.extend(_extract_repeated_blocks(fragment, source_url, source_type))
            found.extend(_extract_inline_label_blocks(fragment, source_url, source_type))
    return found


def _merge_safe_pdf_continuations(lines: list[str]) -> list[str]:
    merged: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if (index + 1 < len(lines)
                and line.count("(") > line.count(")")
                and SAFE_PDF_CONTINUATION.fullmatch(lines[index + 1])):
            line = f"{line} {lines[index + 1]}"
            index += 1
        merged.append(line)
        index += 1
    return merged


def _pdf_strip_bullet(text: str) -> str:
    return PDF_BULLET_PREFIX_PATTERN.sub("", text).strip()


def _pdf_page_blocks(text: str) -> list[tuple[str | None, list[str]]]:
    """Split raw PDF text into page-like blocks, keeping a printed page number.

    ``_extract_pdf_text`` joins page text with a blank line between pages, so
    splitting on blank lines approximates page boundaries. This is what makes
    it possible to reject cross-page adjacency (a heading at the bottom of one
    page followed by the next page's printed number) without inventing page
    numbers that are not actually present in the document.
    """
    blocks: list[tuple[str | None, list[str]]] = []
    for chunk in re.split(r"\n\s*\n", text or ""):
        lines = [_clean(line) for line in chunk.splitlines() if _clean(line)]
        if not lines:
            continue
        page_number = None
        if PDF_PAGE_NUMBER_LINE_PATTERN.fullmatch(lines[0]):
            page_number = lines[0]
            lines = lines[1:]
        lines = [line for line in lines if not PDF_LANGUAGE_MARKER_LINE_PATTERN.fullmatch(line)]
        if lines:
            blocks.append((page_number, lines))
    return blocks


def _pdf_candidate_pairs(lines: list[str]) -> list[tuple[str, str]]:
    """Return structurally pair-shaped (name, value) candidates from one block.

    A candidate is only ever an explicit ``label: value`` line, a digit-free
    label line immediately followed by a line opening with a number, or a
    line that is entirely ``label number+unit`` with nothing left over. Each
    shape requires an explicit unit or colon and never spans an embedded
    digit inside the label, so a sentence that merely mentions a number in
    passing cannot qualify. Lines with a TOC-style dot leader are never
    candidates, since a leader is only ever used to align a heading with a
    page number.
    """
    merged = _merge_safe_pdf_continuations(lines)
    candidates: list[tuple[str, str]] = []
    for index, line in enumerate(merged):
        if PDF_TOC_DOT_LEADER_PATTERN.search(line):
            continue
        match = PDF_PAIR_PATTERN.match(line)
        if match:
            name, value = match.groups()
            candidates.append((_pdf_strip_bullet(name), value))
            continue
        unit_match = PDF_UNIT_TERMINATED_PATTERN.match(line)
        if unit_match:
            name, value = unit_match.groups()
            candidates.append((_pdf_strip_bullet(name), value))
            continue
        if (
            ":" not in line
            and len(line) <= 100
            and not re.search(r"\d", line)
            and index + 1 < len(merged)
        ):
            next_line = merged[index + 1]
            if (
                len(next_line) <= 100
                and not PDF_TOC_DOT_LEADER_PATTERN.search(next_line)
                and re.match(r"^[-+]?\d", next_line)
            ):
                candidates.append((_pdf_strip_bullet(line), next_line))
    return candidates


def _pdf_spec_block_attributes(
    page_number: str | None,
    lines: list[str],
    source_url: str,
    source_type: str | None,
) -> list[RawAttribute]:
    """Extract facts from one page block, only once it proves a spec structure.

    A single accidental colon inside a paragraph (a note, a safety warning, a
    sentence that happens to introduce a list) must not become a fact. The
    block must show either an explicit specification heading or a repeated
    label/value structure before any pair from it is trusted.
    """
    heading = next(
        (
            line for line in lines
            if len(line) <= 60
            and not re.search(r"\d", line)
            and not PDF_TOC_DOT_LEADER_PATTERN.search(line)
            and PDF_SPEC_HEADING_PATTERN.search(line)
        ),
        None,
    )
    plausible: list[tuple[str, str]] = []
    for name, value in _pdf_candidate_pairs(lines):
        name, value = _clean(name), _clean(value)
        if not name or not value or PDF_CONTACT_LABEL_PATTERN.search(name):
            continue
        if _plausible_pair(name, value, generic=True):
            plausible.append((name, value))
    if not plausible:
        return []
    minimum = 1 if heading else 2
    if len(plausible) < minimum or len(plausible) / len(lines) < 0.35:
        return []

    context = " | ".join(part for part in (
        f"page {page_number}" if page_number else None, heading,
    ) if part) or None
    found: list[RawAttribute] = []
    for name, value in plausible:
        item = _attribute(
            name, value, source_url, source_type, "pdf_spec", "medium",
            evidence=f"{name}: {value}", generic=True, context=context,
        )
        if item:
            found.append(item)
    return found


def _extract_pdf_spec_attributes(
    text: str, source_url: str, source_type: str | None,
) -> list[RawAttribute]:
    found: list[RawAttribute] = []
    for page_number, lines in _pdf_page_blocks(text):
        found.extend(_pdf_spec_block_attributes(page_number, lines, source_url, source_type))
    return found


def _pairs_from_lines(text: str, method: str, confidence: str, source_url: str,
                      source_type: str | None) -> list[RawAttribute]:
    lines = [_clean(line) for line in (text or "").splitlines() if _clean(line)]
    if method == "pdf_text":
        lines = _merge_safe_pdf_continuations(lines)
    found = []
    for index, line in enumerate(lines):
        match = PAIR_PATTERN.match(line)
        numeric_match = None if match else NUMERIC_PAIR_PATTERN.match(line)
        match = match or numeric_match
        if match:
            name, raw = match.groups()
            if numeric_match and re.search(r"\d", name):
                continue
            item = _attribute(name, raw, source_url, source_type, method, confidence,
                              f"{_clean(name)} {_clean(raw)}" if method == "pdf_text" else None,
                              generic=True)
            if item:
                found.append(item)
        elif index + 1 < len(lines) and len(line) <= 100 and not re.search(r"\d", line):
            next_line = lines[index + 1]
            if (
                REVIEW_RATING_LINE_PATTERN.match(next_line)
                and index + 2 < len(lines)
                and REVIEW_DATE_LINE_PATTERN.match(lines[index + 2])
            ):
                continue
            if re.match(r"^[-+]?\d", next_line) and len(next_line) <= 100:
                item = _attribute(line.rstrip(":"), next_line, source_url, source_type,
                                  method, confidence, f"{line} {next_line}", generic=True)
                if item:
                    found.append(item)
    return found


def _is_ui_container(tag: Tag) -> bool:
    if tag.name in UI_CONTAINER_NAMES:
        return True
    signals = " ".join([_clean(tag.get("id")), *(_clean(value) for value in tag.get("class", []))])
    return bool(signals and UI_CONTAINER_PATTERN.search(signals))


def _inside_ui_region(tag: Tag) -> bool:
    current: Tag | None = tag
    while isinstance(current, Tag):
        if _is_ui_container(current):
            return True
        current = current.parent if isinstance(current.parent, Tag) else None
    return False


def _high_link_density(tag: Tag) -> bool:
    if tag.name not in {"div", "section", "ul", "ol", "menu"}:
        return False
    links = tag.find_all("a")
    if len(links) < 3:
        return False
    text_length = len(_clean(tag.get_text(" ", strip=True)))
    linked_length = sum(len(_clean(link.get_text(" ", strip=True))) for link in links)
    return text_length > 0 and linked_length / text_length >= 0.65


def _generic_html_text(soup: BeautifulSoup) -> str:
    generic_soup = BeautifulSoup(str(soup), "html.parser")
    for row, _pair in _proven_inline_label_rows(generic_soup):
        row.decompose()
    for tag in list(generic_soup.find_all(True)):
        if tag.parent is None:
            continue
        if tag.name in {"script", "style", "noscript", "template", "svg", "title", "meta",
                        "h1", "h2", "h3", "h4", "h5", "h6"}:
            tag.decompose()
        elif _is_ui_container(tag) or _high_link_density(tag):
            tag.decompose()
    return generic_soup.get_text("\n", strip=True)


def _deduplicate(attributes: Iterable[RawAttribute]) -> list[RawAttribute]:
    attributes = list(attributes)
    strong_names = {
        _clean(item.name).casefold().rstrip(":") for item in attributes
        if METHOD_PRIORITY[item.extraction_method] > METHOD_PRIORITY["spec_block"]
    }
    result: list[RawAttribute] = []
    positions: dict[tuple[str, str], int] = {}
    for item in attributes:
        normalized_name = _clean(item.name).casefold().rstrip(":")
        weak_reconstruction = _clean(f"{item.name} {item.raw_value}").casefold().rstrip(":")
        if item.extraction_method == "spec_block" and (
            normalized_name in strong_names or weak_reconstruction in strong_names
        ):
            continue
        key = (_clean(item.name).casefold().rstrip(":"), _clean(item.raw_value).casefold())
        position = positions.get(key)
        if position is None:
            positions[key] = len(result)
            result.append(item)
        elif METHOD_PRIORITY[item.extraction_method] > METHOD_PRIORITY[result[position].extraction_method]:
            result[position] = item
    return result


def extract_attributes(fetch_result: dict[str, Any]) -> list[RawAttribute]:
    """Extract all structured, plausible raw attributes from a successful fetch."""
    if fetch_result.get("status") != "success":
        return []
    source_url = _clean(fetch_result.get("source_url") or fetch_result.get("final_url"))
    source_type = fetch_result.get("source_type")
    document_type = fetch_result.get("document_type")
    attributes: list[RawAttribute] = []
    if document_type == "pdf":
        attributes.extend(_extract_pdf_spec_attributes(
            fetch_result.get("pdf_text", ""), source_url, source_type,
        ))
        return _deduplicate(attributes)
    if document_type == "html" and fetch_result.get("html"):
        soup = BeautifulSoup(fetch_result["html"], "html.parser")
        attributes.extend(_extract_json_ld(soup, source_url, source_type))
        attributes.extend(_extract_embedded_product_state(soup, source_url, source_type))
        attributes.extend(_extract_structured_data_values(soup, source_url, source_type))
        attributes.extend(_extract_tables(soup, source_url, source_type))
        attributes.extend(_extract_definitions(soup, source_url, source_type))
        attributes.extend(_extract_label_values(soup, source_url, source_type))
        attributes.extend(_extract_repeated_blocks(soup, source_url, source_type))
        attributes.extend(_extract_inline_label_blocks(soup, source_url, source_type))
        attributes.extend(_extract_embedded_structures(soup, source_url, source_type))
        attributes.extend(_pairs_from_lines(_generic_html_text(soup), "spec_block", "low",
                                            source_url, source_type))
    elif document_type == "text":
        attributes.extend(_pairs_from_lines(fetch_result.get("text", ""), "plain_text", "low",
                                            source_url, source_type))
    derived_features: list[RawAttribute] = []
    for item in attributes:
        derived_features.extend(_extract_labeled_feature_measurements(
            item.name,
            item.raw_value,
            item.source_url,
            item.source_type,
            extraction_method=item.extraction_method,
            confidence=item.confidence,
            evidence=item.evidence,
            context=item.context,
        ))
    attributes.extend(derived_features)
    return _deduplicate(attributes)
