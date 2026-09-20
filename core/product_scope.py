"""Conservative category evidence from a fetched primary product object.

This only narrows an already audited host seed. Search titles, snippets,
navigation, and the request text cannot establish a seed's product scope.
"""

from __future__ import annotations

import json
import re

from bs4 import BeautifulSoup

from core.identity import PageIdentityAssessment, base_model_in_text, normalized_identity
from core.sku import requested_sku


_CATEGORY_TERMS = {
    "major appliances": r"\b(?:washing machine|washer|dishwasher|dryer|refrigerator|fridge|freezer|oven|hob|cooktop|plaque de cuisson|four encastrable|фурна|пералня)\b",
    "small appliances": r"\b(?:air ?fryer|coffee machine|coffee maker|vacuum(?: cleaner)?|blender|food processor|stand mixer|kettle|steam iron)\b",
    "networking": r"\b(?:wi-?fi routers?|routers?|mesh (?:system|wi-?fi)|ethernet switch|network switch|access point)\b",
    "computer/peripherals": r"\b(?:keyboard|mouse|scanner|printer|headset|webcam)\b",
    "PC components": r"\b(?:power supply|graphics card|geforce|motherboard|gpu|solid.state drive|ssd|memory module|pc case)\b",
    "power tools": r"\b(?:drill|grinder|circular saw|mitre saw|miter saw|sander|impact driver|rotary hammer)\b",
    "TV/display": r"\b(?:television|tv|display|monitor)\b",
    "apparel/footwear": r"\b(?:shoes?|sneakers?|running vest|running top|t-shirt|jersey|jacket|pants|shorts|shirt)\b",
    "personal care/skincare": r"\b(?:toothbrush|cleanser|facial cream|skin care|skincare|shaver|razor)\b",
    "fishing": r"\b(?:fishing reel|fly reel|fishing rod|fishing lure|ice fishing jig)\b",
}


def _product_objects(soup: BeautifulSoup) -> list[dict]:
    products: list[dict] = []
    for script in soup.find_all("script", attrs={"type": re.compile(r"application/ld\+json", re.I)}):
        raw = script.string or script.get_text() or ""
        if len(raw) > 1_000_000:
            continue
        try:
            pending = [json.loads(raw)]
        except (ValueError, TypeError):
            continue
        while pending:
            node = pending.pop()
            if isinstance(node, list):
                pending.extend(node)
            elif isinstance(node, dict):
                types = node.get("@type", ())
                types = [types] if isinstance(types, str) else types
                if isinstance(types, list) and "product" in {str(item).casefold() for item in types}:
                    products.append(node)
                pending.extend(value for value in node.values() if isinstance(value, (dict, list)))
    return products


def category_from_primary_product(model: str, html: str,
                                  identity: PageIdentityAssessment) -> tuple[str, str]:
    """Return a single scope category only after exact primary identity."""
    if identity.relation != "exact":
        return "unknown", "Primary product identity is not exact"
    soup = BeautifulSoup(html or "", "html.parser")
    signals = [identity.primary_name]
    products = _product_objects(soup)
    sku = requested_sku(model)
    if len(products) == 1:
        product = products[0]
        product_name = str(product.get("name") or "")
        product_codes = " ".join(str(product.get(key) or "") for key in ("sku", "mpn", "model"))
        if (base_model_in_text(model, product_name)
                or bool(sku and sku.full.casefold() in normalized_identity(product_codes))):
            signals.extend((product_name, str(product.get("category") or "")))
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    family = model.replace(sku.raw, "").strip() if sku else ""
    title_has_model = (base_model_in_text(model, title)
                       or bool(sku and sku.full.casefold() in normalized_identity(title))
                       or bool(family and base_model_in_text(family, title)
                               and base_model_in_text(family, identity.primary_name)))
    if title_has_model:
        signals.append(title)
    matched = {
        category for category, pattern in _CATEGORY_TERMS.items()
        if any(re.search(pattern, signal, re.I) for signal in signals)
    }
    if len(matched) == 1:
        category = next(iter(matched))
        return category, f"Fetched primary product identifies {category}"
    return "unknown", "Primary product category is absent or ambiguous"
