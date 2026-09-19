"""Stage 34: raw attribute extraction from already-discovered official sources.

Input is a source (HTML of an official product/support page, or the pages of an
official document) that discovery has already selected.  Output is *raw*
label/value pairs with provenance -- no normalisation, no canonical mapping,
no conflict resolution.

Readers (all generic, no per-site selectors):

* HTML tables, definition lists, label/value element pairs, ``Label: value`` lines;
* the same structures inside hidden containers (``hidden``, ``display:none``,
  closed ``<details>``, inactive tab panes) and inside ``<template>`` / script
  templates -- recorded with a distinct ``location``;
* JSON-LD ``Product`` (``additionalProperty`` and scalar fields);
* JSON state (``__NEXT_DATA__``, ``application/json``, ``window.X = {...}``,
  JSON in ``data-*`` attributes);
* PDF/document text, scoped page by page so that a shared manual never lends
  a neighbouring model's numbers to the requested one.

Neighbouring SKUs are never mixed in: related/recommended/compare blocks,
JSON product nodes and document pages that name a *different* identifier of
the same shape are counted in ``excluded`` instead of being emitted.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from difflib import SequenceMatcher
import json
import re
from typing import Iterable

from bs4 import BeautifulSoup, Comment, NavigableString, Tag
from bs4.element import CData, TemplateString

from core.page_inspection import _ACTIVE_CLASS, _HIDDEN_CLASS
from core.sku import sku_relation

LOCATIONS = ("visible_dom", "dom_hidden", "template", "json_ld", "json_state", "document")


@dataclass(frozen=True, slots=True)
class RawAttribute:
    raw_label: str
    raw_value: str
    source_url: str
    source_type: str          # official_product_page | official_support_page | official_document
    location: str             # one of LOCATIONS
    method: str               # table_row | definition_list | element_pair | label_colon | json_ld_property | ...
    section: str = ""
    document_type: str = ""
    page_ref: str = ""        # PDF page number
    sku_scope: str = "page"   # page | document_model_page | document_scope
    seen_in: tuple[str, ...] = ()  # other locations carrying the identical pair

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(slots=True)
class HtmlExtraction:
    attributes: list[RawAttribute] = field(default_factory=list)
    excluded: dict[str, int] = field(default_factory=dict)

    def exclude(self, reason: str, count: int = 1) -> None:
        self.excluded[reason] = self.excluded.get(reason, 0) + count


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_SPACE = re.compile(r"\s+")
_SPEC_HINT = re.compile(
    r"spec|feature|attribute|propert|characteristic|technical|tech-|param|detail|dimension|classification"
    r"|data-?table|product-?info|facts|характеристик|свойств|основн|описание",
    re.I,
)
_NEIGHBOUR_CONTEXT = re.compile(
    r"related|recommend|similar|carousel|upsell|cross-?sell|also-?(?:viewed|bought)|recently|you-?may"
    r"|compare|comparison|suggest|accessor|bundle|slider-?products|product-?tiles?|product-?grid"
    r"|store-?locator|stockist|find-?a-?store|participating|cookie|consent",
    re.I,
)
_LABEL_CLASS = re.compile(r"(?:^|[\s_-])(?:label|name|title|key|term|attr(?:ibute)?-?name|prop(?:erty)?-?name|head)(?:$|[\s_-])", re.I)
_VALUE_CLASS = re.compile(r"(?:^|[\s_-])(?:value|val|text|desc(?:ription)?|data|attr(?:ibute)?-?value|prop(?:erty)?-?value|content)(?:$|[\s_-])", re.I)
_NOISE_LABEL = re.compile(r"^(?:http|www\.|©|\d+$)|\b(?:cookie|privacy|newsletter|subscribe|login|sign in|add to (?:cart|basket))\b", re.I)
_JUNK_SECTION = re.compile(
    r"promo|\bterms\b|\bconditions\b|shipping|delivery|\breturns?\b|\bstores?\b|\blocations?\b|cookie|privacy"
    r"|newsletter|\bcollection\b|доставк|оплат|подписк",
    re.I,
)
_MAX_LABEL = 90
_MAX_VALUE = 600
_ID_KEYS = re.compile(
    r"^(?:sku|mpn|model(?:number|no|_?id|code)?|product_?id|article_?number|item_?number|code|e_?nr|enr|part_?number)$", re.I,
)


def clean(value: object) -> str:
    return _SPACE.sub(" ", str(value if value is not None else "")).strip()


def _scalar(value: object) -> str:
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, (str, int, float)):
        return clean(value)
    return ""


def _is_pair(label: str, value: str) -> bool:
    if not label or not value or label == value:
        return False
    if len(label) > _MAX_LABEL or len(value) > _MAX_VALUE or len(label) < 2:
        return False
    if _NOISE_LABEL.search(label) or label.count(" ") > 9:
        return False
    if label.endswith(("?", "!")) or "\n" in label:
        return False
    return True


def _compact_id(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", value or "").upper()


def _model_stem(model: str) -> str:
    """Model without a trailing colour/variant letter (``EC685M`` -> ``EC685``)."""
    compact = _compact_id(model)
    stem = re.sub(r"(?<=\d)[A-Z]$", "", compact)
    return stem if stem != compact and len(stem) >= 5 else ""


def sibling_identifier(model: str, text: str) -> str:
    """An identifier in ``text`` shaped like ``model`` but not the same product."""
    wanted = _compact_id(model)
    if len(wanted) < 4:
        return ""
    stem = _model_stem(model)
    relation = sku_relation(model, text)
    for token in re.findall(r"[A-Za-z0-9]+(?:[-_/.][A-Za-z0-9]+)*", text or ""):
        compact = _compact_id(token)
        if compact == wanted or compact == stem or len(compact) < 5 or abs(len(compact) - len(wanted)) > 2:
            continue
        if not (re.search(r"[A-Z]", compact) and re.search(r"\d", compact)):
            continue
        if compact.startswith(wanted) or wanted.startswith(compact):
            return token  # extension / truncation of the same identifier: another variant
        ratio = SequenceMatcher(None, compact, wanted).ratio()
        if ratio >= 0.75 or (compact[:2] == wanted[:2] and ratio >= 0.6):
            return token
    if relation.kind in {"different_variant", "different_suffix"}:
        return relation.evidence
    return ""


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------

def _style_hidden(style: str) -> bool:
    text = (style or "").replace(" ", "").lower()
    return "display:none" in text or "visibility:hidden" in text


def _own_hidden(node: Tag) -> bool:
    attrs = node.attrs
    if "hidden" in attrs and str(attrs.get("hidden")).lower() != "false":
        return True
    if str(attrs.get("aria-hidden", "")).lower() == "true":
        return True
    if _style_hidden(str(attrs.get("style", ""))):
        return True
    classes = " ".join(attrs.get("class", []) or [])
    if classes and _HIDDEN_CLASS.search(classes):
        if node.name in {"div", "section", "ul", "li", "dl", "table", "tbody"} and not _ACTIVE_CLASS.search(f" {classes} "):
            return True
    if node.name == "details" and "open" not in attrs:
        return True
    return False


def _location(node: Tag) -> str:
    hidden = False
    child: Tag | None = None
    for ancestor in [node, *node.parents]:
        if not isinstance(ancestor, Tag):
            continue
        if ancestor.name == "template":
            return "template"
        if ancestor.name == "details" and "open" not in ancestor.attrs and child is not None and child.name == "summary":
            pass  # the summary of a closed <details> stays visible
        elif _own_hidden(ancestor):
            hidden = True
        child = ancestor
    return "dom_hidden" if hidden else "visible_dom"


def _in_neighbour_context(node: Tag) -> bool:
    for ancestor in [node, *node.parents]:
        if not isinstance(ancestor, Tag):
            continue
        if ancestor.name in {"nav", "footer", "header"}:
            return True
        marker = " ".join([*(ancestor.attrs.get("class", []) or []), str(ancestor.attrs.get("id", ""))])
        if marker and _NEIGHBOUR_CONTEXT.search(marker):
            return True
    return False


def _in_spec_context(node: Tag, depth: int = 6) -> bool:
    for ancestor in [node, *list(node.parents)[:depth]]:
        if not isinstance(ancestor, Tag):
            continue
        marker = " ".join([*(ancestor.attrs.get("class", []) or []), str(ancestor.attrs.get("id", ""))])
        if marker and _SPEC_HINT.search(marker):
            return True
    return False


def _section_of(node: Tag) -> str:
    heading = node.find_previous(["h1", "h2", "h3", "h4", "summary", "caption"])
    if heading is None:
        return ""
    text = clean(heading.get_text(" ", strip=True))
    return text[:80] if len(text) <= 80 else ""


# bs4 leaves the text inside <template> out of get_text() unless its string type is asked for
_TEXT_TYPES = (NavigableString, CData, TemplateString)


def _text(node: Tag) -> str:
    for junk in node.find_all(["script", "style", "noscript"]):
        junk.extract()
    return clean(node.get_text(" ", strip=True, types=_TEXT_TYPES))


class _Collector:
    def __init__(self, url: str, source_type: str, model: str, document_type: str = "") -> None:
        self.url, self.source_type, self.model, self.document_type = url, source_type, model, document_type
        self.result = HtmlExtraction()
        self.label_hints: dict[str, str] = {}
        self._index: dict[tuple[str, str], int] = {}

    def add(self, label: str, value: str, location: str, method: str, section: str = "") -> None:
        label, value = clean(label).rstrip(":：").strip(), clean(value)
        if not _is_pair(label, value):
            return
        if not location.startswith("json") and _JUNK_SECTION.search(section):
            self.result.exclude("non_spec_section")
            return
        key = (label.casefold(), value.casefold())
        if key in self._index:
            existing = self.result.attributes[self._index[key]]
            if location != existing.location and location not in existing.seen_in:
                self.result.attributes[self._index[key]] = RawAttribute(
                    **{**asdict(existing), "seen_in": (*existing.seen_in, location)},
                )
            return
        self._index[key] = len(self.result.attributes)
        self.result.attributes.append(RawAttribute(
            raw_label=label, raw_value=value, source_url=self.url, source_type=self.source_type,
            location=location, method=method, section=section, document_type=self.document_type,
        ))


def _cells(row: Tag) -> list[Tag]:
    return [cell for cell in row.find_all(["th", "td"], recursive=False)]


def _read_tables(soup: BeautifulSoup, collector: _Collector) -> None:
    for table in soup.find_all("table"):
        if _in_neighbour_context(table):
            collector.result.exclude("neighbour_context_table")
            continue
        rows = table.find_all("tr")
        header_cells: list[str] = []
        column = None
        if rows:
            first = _cells(rows[0])
            if len(first) > 2 and all(cell.name == "th" for cell in first[1:]) or (
                len(first) > 2 and rows[0].parent is not None and rows[0].parent.name == "thead"
            ):
                header_cells = [_text(cell) for cell in first]
                relations = [sku_relation(collector.model, header) for header in header_cells]
                exact = [i for i, rel in enumerate(relations) if rel.kind in {"exact", "regional_suffix"}]
                if exact:
                    column = exact[0]
                elif any(sibling_identifier(collector.model, header) for header in header_cells):
                    collector.result.exclude("neighbour_model_columns", len(rows) - 1)
                    continue
        section = _section_of(table)
        group = ""
        for row in rows:
            cells = _cells(row)
            if len(cells) < 2:
                continue
            location = _location(row)
            if len(cells) == 2 and not _text(cells[1]):
                heading = _text(cells[0])
                if not heading.startswith(("-", "–", "•")):
                    group = heading  # group heading row ("Drilling capacity" + empty value)
                continue
            if group and clean(_text(cells[0])).startswith(("-", "–", "•")):
                row_section = f"{section} > {group}" if section else group
            else:
                row_section = section
                if not clean(_text(cells[0])).startswith(("-", "–", "•")):
                    group = ""
            if column is not None and column < len(cells):
                if row is rows[0]:
                    continue
                collector.add(_text(cells[0]), _text(cells[column]), location, "table_row_model_column", row_section)
            elif len(cells) == 2:
                collector.add(_text(cells[0]), _text(cells[1]), location, "table_row", row_section)
            elif header_cells and row is not rows[0]:
                collector.add(_text(cells[0]), " | ".join(_text(c) for c in cells[1:]), location, "table_row_multi", row_section)


def _read_definition_lists(soup: BeautifulSoup, collector: _Collector) -> None:
    for dl in soup.find_all("dl"):
        if _in_neighbour_context(dl):
            collector.result.exclude("neighbour_context_dl")
            continue
        section = _section_of(dl)
        term: Tag | None = None
        for child in dl.find_all(["dt", "dd"]):
            if child.name == "dt":
                term = child
            elif term is not None:
                collector.add(_text(term), _text(child), _location(child), "definition_list", section)


def _read_element_pairs(soup: BeautifulSoup, collector: _Collector, cap: int = 2500) -> None:
    """Two-child containers inside spec-like context: (label, value)."""
    count = 0
    for node in soup.find_all(["div", "li", "p", "section", "span", "tr"]):
        if count >= cap:
            break
        children = [c for c in node.children if isinstance(c, Tag) and c.name not in {"script", "style", "svg", "img", "br", "noscript"}]
        if len(children) != 2 or node.name == "tr":
            continue
        # only when the pair is spec-like: hinted context or label/value class names
        first, second = children
        by_class = bool(_LABEL_CLASS.search(" ".join(first.get("class", []) or []))) and bool(
            _VALUE_CLASS.search(" ".join(second.get("class", []) or []))
        )
        if not by_class and not _in_spec_context(node):
            continue
        if first.find(["table", "ul", "ol", "dl"]) or second.find(["table", "ul", "ol", "dl"]):
            continue
        label, value = _text(first), _text(second)
        if not _is_pair(label, value) or len(label) > 50 or len(value) > 200:
            continue
        if re.search(r"add to (?:cart|basket)|\bquantity\b", value, re.I):
            continue
        if _in_neighbour_context(node):
            collector.result.exclude("neighbour_context_pair")
            continue
        count += 1
        collector.add(label, value, _location(node), "element_pair", _section_of(node))


_COLON = re.compile(r"^(?P<l>[^:：\n]{2,60}?)\s*[:：]\s*(?P<v>\S.{0,%d})$" % (_MAX_VALUE - 1))


def _read_label_colon(soup: BeautifulSoup, collector: _Collector, cap: int = 2500) -> None:
    count = 0
    for node in soup.find_all(["li", "p", "div", "span", "td"]):
        if count >= cap:
            break
        if node.find(["li", "p", "div", "table", "ul", "ol"]):
            continue
        strong = node.find(["strong", "b"])
        text = _text(node)
        if not text or len(text) > _MAX_VALUE:
            continue
        match = _COLON.match(text)
        if match is None:
            continue
        label = match.group("l")
        # accept only where the label is emphasised or the context is spec-like;
        # sentences and URLs are not attributes
        emphasised = strong is not None and clean(strong.get_text()).rstrip(":： ") == label.strip()
        if not emphasised and not (_in_spec_context(node) and node.name in {"li", "p", "div"}):
            continue
        if len(label.split()) > 6 or re.search(r"https?$", label):
            continue
        if _in_neighbour_context(node):
            collector.result.exclude("neighbour_context_line")
            continue
        count += 1
        collector.add(label, match.group("v"), _location(node), "label_colon", _section_of(node))


def _walk_ld(node: object, out: list[dict]) -> None:
    if isinstance(node, list):
        for item in node:
            _walk_ld(item, out)
    elif isinstance(node, dict):
        kind = node.get("@type")
        kinds = kind if isinstance(kind, list) else [kind]
        if any(str(k).lower() in {"product", "productmodel", "individualproduct", "vehicle"} for k in kinds):
            out.append(node)
        for key, value in node.items():
            if key != "@type" and isinstance(value, (dict, list)):
                _walk_ld(value, out)


_LD_SCALARS = (
    "name", "sku", "mpn", "gtin", "gtin8", "gtin12", "gtin13", "gtin14", "model", "color", "material",
    "pattern", "size", "category", "countryOfOrigin", "productID", "releaseDate", "audience",
)
_LD_QUANTITY = ("weight", "width", "height", "depth", "length")


def _quantity(value: object) -> str:
    if isinstance(value, dict):
        amount = _scalar(value.get("value") or value.get("minValue"))
        unit = _scalar(value.get("unitText") or value.get("unitCode"))
        return clean(f"{amount} {unit}")
    return _scalar(value)


def _read_json_ld(soup: BeautifulSoup, collector: _Collector) -> None:
    for script in soup.find_all("script", attrs={"type": re.compile(r"ld\+json", re.I)}):
        raw = script.string or script.get_text()
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            try:
                data = json.loads(re.sub(r"[\x00-\x1f]+", " ", raw))
            except (TypeError, ValueError):
                collector.result.exclude("json_ld_unparseable")
                continue
        products: list[dict] = []
        _walk_ld(data, products)
        for product in products:
            identifiers = " ".join(_scalar(product.get(key)) for key in ("sku", "mpn", "model", "productID", "name"))
            relation = sku_relation(collector.model, identifiers)
            if relation.kind in {"absent", "different_variant", "different_suffix"} and sibling_identifier(collector.model, identifiers):
                collector.result.exclude("neighbour_json_ld_product")
                continue
            for key in _LD_SCALARS:
                value = product.get(key)
                if isinstance(value, dict):
                    value = value.get("name")
                collector.add(key, _scalar(value), "json_ld", "json_ld_field")
            brand = product.get("brand")
            collector.add("brand", _scalar(brand.get("name") if isinstance(brand, dict) else brand), "json_ld", "json_ld_field")
            for key in _LD_QUANTITY:
                collector.add(key, _quantity(product.get(key)), "json_ld", "json_ld_field")
            props = product.get("additionalProperty")
            for prop in props if isinstance(props, list) else ([props] if isinstance(props, dict) else []):
                if not isinstance(prop, dict):
                    continue
                value = prop.get("value")
                text = _quantity(value) if isinstance(value, dict) else _scalar(value)
                unit = _scalar(prop.get("unitText"))
                collector.add(_scalar(prop.get("name")), clean(f"{text} {unit}") if unit and unit not in text else text,
                              "json_ld", "json_ld_property")


_STATE_SPEC = re.compile(
    r"spec|attribute|propert|characteristic|technical|tech-?data|classification|dimension|csChapter|facet|feature-?list"
    r"|характеристик|свойств",
    re.I,
)
_STATE_LABEL_KEYS = ("name", "label", "title", "key", "facet", "attributeName", "displayName", "featureName", "propertyName", "heading")
_STATE_VALUE_KEYS = ("value", "values", "displayValue", "formattedValue", "attributeValue", "propertyValue", "val", "description")
_NAME_SUFFIX = re.compile(r"(?:Item|Attribute|Feature|Property|Spec)Name$")
_VALUE_SUFFIX = re.compile(r"Value$")
_MAP_SKIP_KEYS = frozenset(_STATE_LABEL_KEYS) | frozenset(_STATE_VALUE_KEYS) | {"type", "id", "text", "url", "href", "src", "alt"}


def _value_of(item: object) -> str:
    if isinstance(item, dict):
        for key in ("value", "text", "displayValue", "name"):
            if _scalar(item.get(key)):
                return _scalar(item[key])
        for key, value in item.items():
            if re.search(r"(?:Value)?Name$", str(key)) and _scalar(value):
                return _scalar(value)
        return ""
    return _scalar(item)


def _state_values(value: object) -> str:
    if isinstance(value, list):
        return ", ".join(part for part in (_value_of(item) for item in value[:40]) if part)
    return _value_of(value)


def _label_and_value(node: dict) -> tuple[str, str]:
    label = next((_scalar(node[k]) for k in _STATE_LABEL_KEYS if k in node and _scalar(node[k])), "")
    if not label:
        label = next((_scalar(v) for k, v in node.items() if _NAME_SUFFIX.search(str(k)) and _scalar(v)), "")
    value = ""
    for key in _STATE_VALUE_KEYS:
        if key in node and _state_values(node[key]):
            value = _state_values(node[key])
            break
    if not value:
        for key, item in node.items():
            if _VALUE_SUFFIX.search(str(key)) and _state_values(item):
                value = _state_values(item)
                break
    return label, value


def _is_neighbour_json(node: dict, model: str) -> bool:
    ids = " ".join(_scalar(v) for k, v in node.items() if _ID_KEYS.match(str(k)) and isinstance(v, (str, int)))
    if not ids:
        return False
    relation = sku_relation(model, ids).kind
    if relation in {"exact", "regional_suffix", "base_only"}:
        return False
    return relation in {"different_variant", "different_suffix"} or bool(sibling_identifier(model, ids))


_UI_KEY = re.compile(r"(?:Text|Alt|Label|Placeholder|Button|Title|Tab|Heading|Message|Link|Caption)$")
_GROUPED_SPECS = re.compile(r"grouped.*specs?$|spec.?groups?$", re.I)
_HINT_LABEL = re.compile(r'"(?:tech_?spec|spec|attr(?:ibute)?)_(?P<key>[\w-]{2,60})"\s*:\s*\{\s*"defaultMessage"\s*:\s*"(?P<label>[^"]{1,80})"')


def _keyed_specs(node: dict, collector: _Collector) -> None:
    """``{"groupedTechSpecs": {group: [keys]}, "c_key": value, ...}``: the list names which keys of
    the same record are specifications; labels come from a ``tech_spec_<key>`` message dictionary."""
    for key, groups in node.items():
        if not (_GROUPED_SPECS.search(str(key)) and isinstance(groups, dict)):
            continue
        for group, names in groups.items():
            if not isinstance(names, list):
                continue
            for name in names:
                if not isinstance(name, str):
                    continue
                for candidate in (name, f"c_{name}", name[:1].lower() + name[1:]):
                    value = node.get(candidate)
                    if isinstance(value, (str, int, float, bool)) and _scalar(value):
                        label = collector.label_hints.get(name) or name
                        collector.add(label, _scalar(value), "json_state", "json_state_keyed_spec", str(group))
                        break


def _walk_state(node: object, path: tuple[str, ...], collector: _Collector, budget: list[int]) -> None:
    if budget[0] <= 0:
        return
    budget[0] -= 1
    spec_path = any(_STATE_SPEC.search(key) for key in path[-6:])
    if isinstance(node, list):
        for item in node[:400]:
            _walk_state(item, path, collector, budget)
        return
    if not isinstance(node, dict):
        return
    if _is_neighbour_json(node, collector.model):
        collector.result.exclude("neighbour_json_product")
        return
    _keyed_specs(node, collector)
    if spec_path:
        label, value = _label_and_value(node)
        if label and value and len(node) <= 16 and len(label) <= 80:
            collector.add(label, value, "json_state", "json_state_pair", " > ".join(path[-2:]))
        elif (
            len(node) >= 2 and path and _STATE_SPEC.search(path[-1])
            and all(not isinstance(v, (dict, list)) for v in node.values())
        ):
            for key, item in node.items():
                if isinstance(item, bool) or str(key) in _MAP_SKIP_KEYS or _UI_KEY.search(str(key)):
                    continue
                text = _scalar(item)
                if text and len(text) <= 300 and not text.startswith(("http", "/")):
                    collector.add(str(key), text, "json_state", "json_state_map", path[-1])
    for key, item in node.items():
        if isinstance(item, (dict, list)):
            _walk_state(item, (*path, str(key)), collector, budget)


_JS_ASSIGN = re.compile(r"(?:window\.|self\.|var\s+|let\s+|const\s+)?[\w.$\[\]\"']{2,60}\s*=\s*(?=[{\[])")
_SPEC_ANCHOR = re.compile(
    r"(?P<key>\"?[A-Za-z_$]?[\w$-]{0,40}?(?:[Ss]pecs?|[Ss]pecifications?|[Cc]haracteristics|csChapter|additionalProperty"
    r"|[Tt]echnicalData|[Cc]lassifications?|[Pp]roductAttributes|[Ff]eatureGroups?)\"?)\s*:\s*(?=[\[{])"
)
_NEXT_PUSH = re.compile(r'self\.__next_f\.push\(\[\d+,\s*"((?:[^"\\]|\\.)*)"\s*\]\)')


def _balanced_end(text: str, start: int, limit: int = 3_000_000) -> int:
    """Index just past the bracket group opening at ``start`` (strings respected), else -1."""
    depth, quote, escaped = 0, "", False
    for index in range(start, min(len(text), start + limit)):
        char = text[index]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
            continue
        if char in "\"'":
            quote = char
        elif char in "[{":
            depth += 1
        elif char in "]}":
            depth -= 1
            if depth == 0:
                return index + 1
    return -1


def _js_to_json(text: str) -> str:
    """Tolerant JS-object-literal -> JSON (bare keys, void 0, !0/!1, single quotes)."""
    out: list[str] = []
    index, size = 0, len(text)
    while index < size:
        char = text[index]
        if char in "\"'":
            end = index + 1
            while end < size and text[end] != char:
                end += 2 if text[end] == "\\" else 1
            body = text[index + 1:end]
            out.append(json.dumps(body.replace("\\'", "'")) if char == "'" else text[index:end + 1])
            index = end + 1
            continue
        match = re.match(r"[A-Za-z_$][\w$]*", text[index:index + 80])
        if match:
            word = match.group(0)
            rest = text[index + len(word):index + len(word) + 20].lstrip()
            if rest.startswith(":"):
                out.append(json.dumps(word))
            elif word in {"true", "false", "null"}:
                out.append(word)
            elif word == "void":
                out.append("null")
                skip = re.match(r"\s*0", text[index + 4:index + 10])
                index += 4 + (skip.end() if skip else 0)
                continue
            else:
                out.append("null")  # undefined / NaN / identifiers
            index += len(word)
            continue
        if char == "!" and text[index:index + 2] in {"!0", "!1"}:
            out.append("true" if text[index + 1] == "0" else "false")
            index += 2
            continue
        out.append(char)
        index += 1
    return "".join(out)


def _load_json_or_js(text: str) -> object | None:
    try:
        return json.loads(text)
    except ValueError:
        pass
    try:
        return json.loads(_js_to_json(text))
    except (ValueError, RecursionError):
        return None


def _json_from_script(text: str) -> list[object]:
    text = (text or "").strip()
    if not text:
        return []
    if text[0] in "{[":
        data = _load_json_or_js(text)
        if data is not None:
            return [data]
    found: list[object] = []
    decoder = json.JSONDecoder()
    for match in list(_JS_ASSIGN.finditer(text))[:6]:
        try:
            value, _ = decoder.raw_decode(text, match.end())
        except ValueError:
            continue
        found.append(value)
    return found


def _script_texts(body: str) -> list[str]:
    """The script text itself plus, for Next.js flight pushes, the decoded payload."""
    texts = [body]
    pushes = _NEXT_PUSH.findall(body)
    if pushes:
        decoded = []
        for payload in pushes:
            try:
                decoded.append(json.loads(f'"{payload}"'))
            except ValueError:
                continue
        texts.append("".join(decoded))
    return texts


def _anchored_state(text: str, collector: _Collector, budget: list[int]) -> int:
    """Decode JSON / JS-literal values that hang off a spec-like key anywhere in ``text``."""
    blocks, covered = 0, -1
    for match in list(_SPEC_ANCHOR.finditer(text))[:60]:
        if match.start() < covered:
            continue
        start = match.end()
        end = _balanced_end(text, start)
        if end < 0:
            continue
        data = _load_json_or_js(text[start:end])
        if data is None:
            continue
        covered = end
        blocks += 1
        _walk_state(data, (match.group("key").strip('"'),), collector, budget)
    return blocks


def _read_json_state(soup: BeautifulSoup, collector: _Collector) -> int:
    blocks = 0
    budget = [80_000]
    for script in soup.find_all("script"):
        kind = str(script.get("type", "")).lower()
        if "ld+json" in kind or kind in {"text/template", "text/x-template"} or script.get("src"):
            continue
        body = script.string or script.get_text() or ""
        if len(body) < 40 or not _STATE_SPEC.search(body[:3_000_000]):
            continue
        if kind not in {"", "application/json", "text/javascript", "application/javascript", "module"} and "json" not in kind:
            continue
        for hint in _HINT_LABEL.finditer(body[:6_000_000]):
            collector.label_hints.setdefault(hint.group("key"), hint.group("label"))
        for text in _script_texts(body[:6_000_000]):
            blocks += _anchored_state(text, collector, budget)
        for data in _json_from_script(body[:6_000_000]):
            blocks += 1
            _walk_state(data, (), collector, budget)
    # JSON carried in data-* attributes
    for node in soup.find_all(True):
        for name, value in node.attrs.items():
            if name.startswith("data-") and isinstance(value, str) and len(value) > 60 and value[:1] in "{[" and _STATE_SPEC.search(value):
                data = _load_json_or_js(value)
                if data is None:
                    continue
                blocks += 1
                _walk_state(data, (name,), collector, budget)
    return blocks


def _read_script_templates(soup: BeautifulSoup, collector: _Collector, model: str, depth: int) -> None:
    for script in soup.find_all("script", attrs={"type": re.compile(r"text/(?:x-)?template|text/html|x-handlebars", re.I)}):
        inner = script.string or script.get_text() or ""
        if len(inner) < 30 or depth > 1:
            continue
        nested = BeautifulSoup(inner, "html.parser")
        sub = _Collector(collector.url, collector.source_type, model, collector.document_type)
        _read_html_structures(nested, sub, model, depth + 1)
        for attribute in sub.result.attributes:
            collector.add(attribute.raw_label, attribute.raw_value, "template", attribute.method + "+script_template", attribute.section)
        for reason, count in sub.result.excluded.items():
            collector.result.exclude(reason, count)


def _read_html_structures(soup: BeautifulSoup, collector: _Collector, model: str, depth: int = 0) -> None:
    _read_tables(soup, collector)
    _read_definition_lists(soup, collector)
    _read_element_pairs(soup, collector)
    _read_label_colon(soup, collector)
    _read_script_templates(soup, collector, model, depth)


def extract_html_attributes(
    html: str, url: str, model: str, source_type: str = "official_product_page", document_type: str = "",
) -> tuple[HtmlExtraction, dict[str, object]]:
    """All raw label/value pairs a delivered HTML document carries."""
    try:
        soup = BeautifulSoup(html or "", "lxml")
    except Exception:  # noqa: BLE001 - lxml missing/failing: fall back to the stdlib parser
        soup = BeautifulSoup(html or "", "html.parser")
    for comment in soup.find_all(string=lambda item: isinstance(item, Comment)):
        comment.extract()
    collector = _Collector(url, source_type, model, document_type)
    # JSON first: it is the cleanest structured source and wins provenance ties
    _read_json_ld(soup, collector)
    json_blocks = _read_json_state(soup, collector)
    _read_html_structures(soup, collector, model)
    stats = {
        "json_state_blocks": json_blocks,
        "by_location": {loc: sum(1 for a in collector.result.attributes if a.location == loc) for loc in LOCATIONS
                        if any(a.location == loc for a in collector.result.attributes)},
        "by_method": _count(a.method for a in collector.result.attributes),
    }
    return collector.result, stats


def _count(values: Iterable[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return counts


# ---------------------------------------------------------------------------
# Documents (PDF text, page by page)
# ---------------------------------------------------------------------------

_TECH_HEADING = re.compile(
    r"^\s*(?:\d+(?:\.\d+)*[.)]?\s*)?(?:technical\s+(?:data|specifications?)|specifications?|technische\s+daten|donn[ée]es\s+techniques"
    r"|caract[ée]ristiques(?:\s+techniques)?|dati\s+tecnici|datos\s+t[ée]cnicos|technische\s+gegevens"
    r"|технические\s+характеристики|технические\s+данные|характеристики|основные\s+технические\s+данные)\b[^.]{0,40}$",
    re.I,
)
_UNIT_VALUE = re.compile(
    r"^(?P<l>[^\d:]{3,60}?)\s{1,}(?P<v>\d[\d.,]*(?:\s?[-–x×/]\s?\d[\d.,]*)*\s?(?:[A-Za-zА-Яа-яµ°Ω%/²³.]{1,10}\b.{0,60})?)$"
)
_DOC_COLON = re.compile(r"^(?P<l>[A-Za-zА-Яа-яÀ-ÿ][^:：\n]{1,58}?)\s*[:：]\s*(?P<v>\S.{0,200})$")
_PROSE = re.compile(r"[.!?]\s+[A-ZА-Я]|\b(?:please|make sure|do not|never|always|must|should|bitte|nicht|необходимо|нельзя)\b", re.I)
_DANGLING = re.compile(r"(?:^|\s)(?:от|до|в|на|с|по|за|для|при|of|to|in|at|for|and|und|de|du|le)\s*$", re.I)
_BULLET = re.compile(r"^[•·▪■\-–*]\s*")
_NOTE_LABEL = re.compile(r"^(?:note|notes|warning|caution|attention|important|tip|примечание|внимание|важно|hinweis|achtung)$", re.I)
# Purposes whose prose is instructions: only their technical-data sections are attribute-bearing.
_PROSE_DOCUMENT_TYPES = frozenset({"manual", "instruction", "user_guide", "quick_start_guide"})


def _document_lines(text: str, state: dict[str, int], tech_only: bool) -> list[tuple[str, str, str]]:
    """(label, value, method) triples found in one page of document text.

    ``state['tech']`` counts the lines left in the current technical-data section
    and survives across pages (sections run over page breaks).
    """
    out: list[tuple[str, str, str]] = []
    for raw in text.splitlines():
        line = _BULLET.sub("", clean(raw))
        if not line:
            continue
        if len(line) <= 70 and _TECH_HEADING.match(line) and not re.search(r"(?:\.{3,}|\s\d+)\s*$", line) and not re.search(r"\d\s*(?:mm|kg|v|w|hz)\b", line, re.I):
            state["tech"] = 45
            continue
        in_tech = state["tech"] > 0
        state["tech"] = max(0, state["tech"] - 1)
        if tech_only and not in_tech:
            continue
        match = _DOC_COLON.match(line)
        if (
            match and len(match.group("l").split()) <= 6 and not _PROSE.search(line)
            and not re.search(r"https?|www\.", match.group("l")) and not _NOTE_LABEL.match(match.group("l").strip())
        ):
            out.append((match.group("l"), match.group("v"), "pdf_label_colon"))
        elif in_tech:
            unit = _UNIT_VALUE.match(line)
            if unit and len(unit.group("l").split()) <= 7 and not _PROSE.search(line) and not _DANGLING.search(unit.group("l")):
                out.append((unit.group("l"), unit.group("v"), "pdf_tech_section_line"))
    return out


def _page_has_model(model: str, text: str) -> bool:
    if sku_relation(model, text).kind in {"exact", "regional_suffix", "base_only"}:
        return True
    stem = _model_stem(model)
    if stem:  # "EC685" for EC685M: the family name a manual uses for a colour/variant letter
        return any(_compact_id(token) == stem for token in re.findall(r"[A-Za-z0-9]+(?:[-_/.][A-Za-z0-9]+)*", text))
    return False


def scope_document_pages(pages: list[str], model: str) -> tuple[list[tuple[int, str, str]], dict[str, object]]:
    """Select the pages of a document that may feed the requested model.

    Returns ``[(page_number, sku_scope, text)]`` and a report.  A page that names
    a sibling identifier without carrying the requested one is never used; a page
    naming both is shared and excluded (its columns cannot be separated from text).
    """
    info: list[dict[str, object]] = []
    for number, text in enumerate(pages, 1):
        info.append({
            "page": number, "text": text,
            "has_model": _page_has_model(model, text),
            "sibling": sibling_identifier(model, text),
        })
    model_pages = [item for item in info if item["has_model"]]
    doc_has_sibling = any(item["sibling"] for item in info)
    report: dict[str, object] = {
        "pages": len(pages), "model_pages": len(model_pages), "sibling_pages": sum(1 for i in info if i["sibling"]),
        "model_in_text": bool(model_pages), "multi_model_document": doc_has_sibling,
        "sibling_examples": sorted({str(i["sibling"]) for i in info if i["sibling"]})[:6],
    }
    selected: list[tuple[int, str, str]] = []
    shared = ambiguous = 0
    for item in info:
        text = str(item["text"])
        if item["has_model"] and not item["sibling"]:
            selected.append((int(item["page"]), "document_model_page", text))
        elif item["has_model"] and item["sibling"]:
            shared += 1
        elif not doc_has_sibling:
            selected.append((int(item["page"]), "document_scope", text))
        else:
            ambiguous += 1
    report["excluded_shared_pages"] = shared
    report["excluded_ambiguous_pages"] = ambiguous
    return selected, report


def extract_document_attributes(
    pages: list[str], url: str, model: str, document_type: str, discovery_model_match: str,
) -> tuple[HtmlExtraction, dict[str, object]]:
    result = HtmlExtraction()
    selected, report = scope_document_pages(pages, model)
    if not report["model_in_text"]:
        # The document never names the requested model: whatever it lists cannot be
        # attributed to this SKU (discovery's URL/title match is not enough).
        result.exclude("document_model_not_in_text", sum(len(_document_lines(t, {"tech": 0}, False)) for _, _, t in selected))
        report["by_method"] = {}
        return result, report
    seen: set[tuple[str, str]] = set()
    state = {"tech": 0}
    tech_only = document_type in _PROSE_DOCUMENT_TYPES
    for number, scope, text in selected:
        for label, value, method in _document_lines(text, state, tech_only):
            if not _is_pair(label, value) or (label.casefold(), value.casefold()) in seen:
                continue
            seen.add((label.casefold(), value.casefold()))
            result.attributes.append(RawAttribute(
                raw_label=label.strip(), raw_value=value, source_url=url, source_type="official_document",
                location="document", method=method, document_type=document_type,
                page_ref=str(number), sku_scope=scope,
            ))
    if report["excluded_shared_pages"]:
        result.exclude("neighbour_sku_shared_page", int(report["excluded_shared_pages"]))
    if report["excluded_ambiguous_pages"]:
        result.exclude("neighbour_sku_ambiguous_page", int(report["excluded_ambiguous_pages"]))
    report["by_method"] = _count(a.method for a in result.attributes)
    return result, report
