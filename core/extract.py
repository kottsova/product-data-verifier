"""Extract raw, source-faithful product attributes from a FetchResult."""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Any, Iterable

from bs4 import BeautifulSoup, Tag

METHOD_PRIORITY = {"json_ld": 7, "html_table": 6, "definition_list": 5,
                   "label_value": 4, "spec_block": 3, "pdf_text": 2, "plain_text": 1}
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
UNIT_TOKEN = r"mm|cm|km|m|mg|kg|g|lb|oz|ml|cl|l|mAh|Ah|kW|W|V|A|Hz|MHz|GHz|GB|TB|MB|°C|°F|%|rpm|dB"
UNIT_PATTERN = re.compile(
    rf"^\s*([-+]?\d+(?:[.,]\d+)?(?:\s*(?:[x×;]|[-–—])\s*[-+]?\d+(?:[.,]\d+)?)*)\s*({UNIT_TOKEN})\s*$",
    re.IGNORECASE,
)
PAIR_PATTERN = re.compile(r"^(.{1,120}?)(?:\s*:\s*|\s+\.{2,}\s*)(.{1,500})$")
NUMERIC_PAIR_PATTERN = re.compile(
    r"^([^:]{1,100}?[A-Za-zА-Яа-я])\s+([-+]?\d+(?:[.,]\d+)?(?:\s*[x×]\s*\d+(?:[.,]\d+)?){0,3}(?:\s*[A-Za-z°%]+)?)$"
)
FACTUAL_JSON_FIELDS = ("sku", "mpn", "gtin", "gtin8", "gtin12", "gtin13", "gtin14",
                       "productID", "model", "brand", "color", "material", "weight",
                       "width", "height", "depth", "size", "category")
OFFER_FIELDS = ("price", "priceCurrency", "availability", "itemCondition")
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
    if method in {"json_ld", "html_table", "label_value", "pdf_text"}:
        return "product"
    return "unknown"


def _attribute(name: Any, raw_value: Any, source_url: str, source_type: str | None,
               method: str, confidence: str, evidence: str | None = None,
               explicit_unit: str | None = None, *, generic: bool = False,
               attribute_kind: str | None = None) -> RawAttribute | None:
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
                        _attribute_kind(name, raw_value, method, attribute_kind))


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
        item = _attribute(name, raw, source_url, source_type, "html_table", "high")
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
                              source_url, source_type, "definition_list", "high")
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
                              source_url, source_type, "label_value", "medium", generic=True)
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
                              "label_value", "medium", generic=True)
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
        weak_reconstruction = _clean(f"{item.name} {item.raw_value}").casefold().rstrip(":")
        if item.extraction_method == "spec_block" and weak_reconstruction in strong_names:
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
        attributes.extend(_pairs_from_lines(fetch_result.get("pdf_text", ""), "pdf_text", "medium",
                                            source_url, source_type))
        return _deduplicate(attributes)
    if document_type == "html" and fetch_result.get("html"):
        soup = BeautifulSoup(fetch_result["html"], "html.parser")
        attributes.extend(_extract_json_ld(soup, source_url, source_type))
        attributes.extend(_extract_tables(soup, source_url, source_type))
        attributes.extend(_extract_definitions(soup, source_url, source_type))
        attributes.extend(_extract_label_values(soup, source_url, source_type))
        attributes.extend(_extract_repeated_blocks(soup, source_url, source_type))
        attributes.extend(_extract_embedded_structures(soup, source_url, source_type))
        attributes.extend(_pairs_from_lines(_generic_html_text(soup), "spec_block", "low",
                                            source_url, source_type))
    elif document_type == "text":
        attributes.extend(_pairs_from_lines(fetch_result.get("text", ""), "plain_text", "low",
                                            source_url, source_type))
    return _deduplicate(attributes)
