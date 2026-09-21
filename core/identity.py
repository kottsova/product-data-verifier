"""Conservative parsing and comparison of product identity strings."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import re
import unicodedata
from typing import Iterable, Literal, Mapping

from urllib.parse import unquote, urlparse


MarketScope = Literal["global", "regional", "unknown"]
IdentityConfidence = Literal["high", "medium", "low"]
IdentityRelation = Literal[
    "exact_variant",
    "same_base_model",
    "probable_same_base_model",
    "different_model",
    "unknown",
]
AttributeScope = Literal["model_level", "variant_level", "market_level", "unknown"]
ATTRIBUTE_SCOPES = {"model_level", "variant_level", "market_level", "unknown"}

MARKET_SCOPES = {"global", "regional", "unknown"}
CONFIDENCE_LEVELS = {"high", "medium", "low"}
RELATIONS = {
    "exact_variant", "same_base_model", "probable_same_base_model",
    "different_model", "unknown",
}
IDENTITY_EVIDENCE_FIELDS = {
    "brand", "base_model", "commercial_model", "manufacturer_article", "product_code",
    "sku", "gtin", "color", "market_hint", "market_scope",
}

COLOR_NAMES = {
    "beige", "black", "blue", "bronze", "brown", "cream", "cyan", "gold",
    "gray", "green", "grey", "orange", "pink", "purple", "red", "rose",
    "silver", "violet", "white", "yellow",
}
MODEL_EXTENSION_WORDS = {
    "air", "lite", "max", "mini", "plus", "pro", "ultra",
}

CONFIGURATION_RE = re.compile(
    r"(?<![\w])(?P<ram>\d{1,2})\s*(?:GB\s*)?(?:\+|/)\s*"
    r"(?P<storage>\d{2,4})\s*(?:GB)?(?![\w])",
    re.IGNORECASE,
)
SPACED_CONFIGURATION_RE = re.compile(
    r"(?<![\w])(?P<ram>\d{1,2})\s*GB\s*[+/ ]\s*"
    r"(?P<storage>\d{2,4})\s*GB(?![\w])",
    re.IGNORECASE,
)
PLAUSIBLE_RAM = {2, 3, 4, 6, 8, 12, 16, 18, 24, 32}
PLAUSIBLE_STORAGE = {32, 64, 128, 256, 512, 1024, 2048}


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = " ".join(unicodedata.normalize("NFKC", str(value)).split())
    return cleaned or None


def normalized_identity(value: str | None) -> str:
    """Return a safe comparison key without changing the stored raw value."""
    text = unicodedata.normalize("NFKC", value or "").casefold()
    return "".join(character for character in text if character.isalnum())


def base_model_in_text(base_model: str | None, text: str | None) -> bool:
    """Match a complete model phrase while preserving meaningful word extensions."""
    parts = re.findall(r"[\w]+", unicodedata.normalize("NFKC", base_model or ""))
    if not parts or not text:
        return False
    separator = r"[\s_-]*"
    pattern = re.compile(
        rf"(?<!\w){separator.join(re.escape(part) for part in parts)}(?!\w)",
        re.IGNORECASE,
    )
    for match in pattern.finditer(unicodedata.normalize("NFKC", text)):
        following = re.match(r"[\s_-]+([A-Za-z]+)", text[match.end():])
        if following and following.group(1).casefold() in MODEL_EXTENSION_WORDS:
            continue
        return True
    return False


@dataclass(frozen=True, slots=True)
class PageIdentityAssessment:
    """Identity of the page's main product, never inferred from a search query."""

    relation: Literal["exact", "different_variant", "related_item", "unknown"]
    primary_name: str
    evidence: str


def assess_product_page_identity(model: str, html: str, final_url: str) -> PageIdentityAssessment:
    """Check the main page object instead of treating a URL or snippet as proof.

    Only a primary heading or single Product structured-data object counts.
    Body-wide matches are deliberately ignored: a parts page and a category
    page can mention the requested model many times without being that model.
    """
    from bs4 import BeautifulSoup

    from core.match import candidate_model_match
    from core.sku import requested_sku, sku_relation

    soup = BeautifulSoup(html or "", "html.parser")
    heading = soup.find("h1")
    primary = heading.get_text(" ", strip=True) if heading else ""
    structured: list[dict] = []
    for script in soup.find_all("script", attrs={"type": re.compile(r"application/ld\+json", re.I)}):
        raw = script.string or script.get_text() or ""
        if len(raw) > 1_000_000:
            continue
        try:
            pending = [json.loads(raw)]
        except (ValueError, TypeError):
            continue
        while pending:
            item = pending.pop()
            if isinstance(item, list):
                pending.extend(item)
            elif isinstance(item, dict):
                types = item.get("@type", ())
                types = [types] if isinstance(types, str) else types
                if isinstance(types, list) and "product" in {str(t).casefold() for t in types}:
                    structured.append(item)
                pending.extend(value for value in item.values() if isinstance(value, (dict, list)))
    if not primary and len(structured) == 1:
        primary = str(structured[0].get("name") or "").strip()
    if not primary:
        return PageIdentityAssessment("unknown", "", "No primary product heading or unique Product object")

    # Some manufacturer templates put the descriptive name in <h1> and the
    # commercial code in their one Product object.  Keep the heading as the
    # main-object guard; use only a unique Product object whose identifier and
    # destination URL both carry the complete requested SKU.
    requested = requested_sku(model)
    if requested is not None and heading is not None:
        # Some product pages use a family/base code in <h1> while the actual
        # commercial reference is printed directly beneath that heading.
        # This nearby detail can veto an exact SKU; it must never establish
        # identity from a body-wide match or a related-products section.
        nearby: list[str] = []
        size = 0
        for node in heading.next_elements:
            if getattr(node, "name", None) == "h1":
                break
            if (getattr(node, "name", None) == "h2"
                    and not base_model_in_text(model, node.get_text(" ", strip=True))):
                break
            if not isinstance(node, str) or node.parent.name in {"script", "style"}:
                continue
            value = str(node).strip()
            if not value:
                continue
            nearby.append(value)
            size += len(value)
            if size >= 1200:
                break
        references = set(re.findall(
            r"\bReference\s*:\s*([A-Z0-9][A-Z0-9._/-]{4,})\b",
            " ".join(nearby), re.I,
        ))
        if len(references) == 1:
            reference_relation = sku_relation(model, next(iter(references)))
            if reference_relation.kind in {"different_suffix", "different_variant"}:
                return PageIdentityAssessment(
                    "different_variant", primary,
                    "Primary product reference names a different commercial variant",
                )
    if requested is not None and len(structured) == 1:
        product = structured[0]
        product_code = " ".join(str(product.get(key) or "") for key in ("sku", "mpn", "model"))
        product_name = str(product.get("name") or "").strip()
        path_code = normalized_identity(unquote(urlparse(final_url).path))
        if (sku_relation(model, product_code).kind == "exact"
                and requested.full.casefold() in path_code
                and not re.search(r"\b(?:accessory|dust\s+bag|brush|filter|refill|twin[ -]?pack)\b", primary, re.I)):
            primary = f"{primary} {product_name} {product_code}".strip()
    if requested is not None and sku_relation(model, primary).kind == "absent":
        # Apparel sites often put a style/product code in a labelled detail
        # next to a family-level heading.  Require one unambiguous code, the
        # family name in that heading, and the same complete code in the URL.
        labelled = re.findall(
            r"\b(?:Style|Product\s+Code)\s*:\s*([A-Z0-9][A-Z0-9._/-]{4,})\b",
            soup.get_text(" ", strip=True), flags=re.I,
        )
        labelled_relations = {sku_relation(model, value).kind for value in labelled}
        family_words = re.findall(r"[A-Za-z]+", model.replace(requested.raw, ""))
        if (labelled and labelled_relations == {"exact"}
                and requested.full.casefold() in normalized_identity(unquote(urlparse(final_url).path))
                and (not family_words or family_words[0].casefold() in primary.casefold())):
            primary = f"{primary} {requested.raw}"

    # A title of the form "brush for Model X" explicitly names a different
    # main object, regardless of how often Model X appears in the page.
    compatibility = re.search(r"\b(?:for|compatible\s+with|fits|replacement\s+for)\b", primary, re.I)
    document_heading = bool(re.search(r"\b(?:manual|support|specifications|service|datasheet)\b", primary[:compatibility.start()] if compatibility else "", re.I))
    if compatibility and not document_heading and base_model_in_text(model, primary[compatibility.end():]):
        return PageIdentityAssessment("related_item", primary, "Main object is for/compatible with requested model")

    configuration = re.compile(r"\b(?:refill|twin[ -]?pack|bundle|combo|kit|for[ -]mac)\b", re.I)
    product_path = unquote(urlparse(final_url).path).replace("-", " ").replace("_", " ")
    if (configuration.search(f"{primary} {product_path}")
            and not configuration.search(model)):
        return PageIdentityAssessment("different_variant", primary, "Main object names a different package or refill")

    # Unknown future variant names need not be enumerated. A distinctive
    # suffix immediately after the requested model in *both* the primary
    # product name and destination slug is positive evidence of a different
    # commercial item. Generic descriptors such as "Washing machine" do not
    # have this acronym/CamelCase shape.
    model_parts = re.findall(r"[A-Za-z0-9]+", model)
    phrase = re.compile(r"(?<!\w)" + r"[\s_-]*".join(map(re.escape, model_parts)) + r"(?!\w)", re.I) if model_parts else None
    occurrence = phrase.search(primary) if phrase else None
    if occurrence:
        tail = primary[occurrence.end():]
        # Product header containers sometimes include a review score after
        # the SKU. A score such as "4.7 (265)" is page chrome.
        rating_tail = bool(re.match(r"\s+[0-5][.,]\d\s*\(\d+\)", tail))
        voltage_tail = bool(re.match(r"\s+\d{1,3}\s?V(?:max)?\b", tail, re.I))
        following = None if rating_tail or voltage_tail else re.match(r"[\s:–-]+([A-Za-z0-9]+)", tail)
        if following:
            suffix = following.group(1)
            distinctive = suffix.isupper() or suffix.isdigit() or any(ch.isupper() for ch in suffix[1:])
            slug_parts = [part.casefold() for part in re.findall(r"[A-Za-z0-9]+", unquote(urlparse(final_url).path))]
            expected_parts = [part.casefold() for part in model_parts]
            follows_in_slug = any(
                slug_parts[index:index + len(expected_parts)] == expected_parts
                and index + len(expected_parts) < len(slug_parts)
                and slug_parts[index + len(expected_parts)] == suffix.casefold()
                for index in range(len(slug_parts))
            )
            # A market-looking suffix alone is insufficient to assert a
            # distinct commercial variant without an explicit product SKU.
            if distinctive and not re.fullmatch(r"[A-Z]{2}", suffix):
                if follows_in_slug:
                    return PageIdentityAssessment("different_variant", primary, "Main product and URL name an extra commercial variant")
                return PageIdentityAssessment("unknown", primary, "Main product names an extra variant absent from the URL")

    if occurrence and re.match(r"-([A-Z]{2})(?![A-Za-z0-9])", primary[occurrence.end():]):
        return PageIdentityAssessment("unknown", primary, "Regional-looking suffix needs an explicit commercial identifier")

    path = unquote(urlparse(final_url).path)
    match = candidate_model_match(model, primary, path)
    if match in {"different_variant", "mismatch"}:
        return PageIdentityAssessment("different_variant", primary, "Main product identifies a different model or variant")

    sku = requested_sku(model)
    if sku is not None:
        # A regional-looking suffix is a hypothesis until an explicit product
        # identifier agrees. The URL may contain navigation/compatibility text,
        # so the primary object takes precedence.
        parts = [primary]
        if len(structured) == 1:
            parts.extend(str(structured[0].get(key) or "") for key in ("sku", "mpn", "model"))
        relation = sku_relation(model, *parts)
        if relation.kind in {"different_suffix", "different_variant"}:
            return PageIdentityAssessment("different_variant", primary, f"Product SKU relation: {relation.kind}")
        if relation.kind == "absent":
            # Product headings sometimes typeset one identifier with spaces
            # ("MQ 9187XLI") while the request and product URL use the
            # compact SKU ("MQ9187XLI"). Require the *whole* code in the
            # main heading and URL, with alphanumeric boundaries, before
            # accepting this formatting difference.
            spaced_code = re.compile(
                r"(?<![A-Za-z0-9])" + r"[\s_-]*".join(map(re.escape, sku.full))
                + r"(?![A-Za-z0-9])", re.I,
            )
            family_words = re.findall(r"[A-Za-z]+", model.replace(sku.raw, ""))
            family_present = not family_words or family_words[0].casefold() in primary.casefold()
            if spaced_code.search(primary) and sku.full.casefold() in normalized_identity(path) and family_present:
                return PageIdentityAssessment("exact", primary, "Main product and URL give the same complete SKU with spacing variation")
        if relation.kind != "exact":
            return PageIdentityAssessment("unknown", primary, f"Product SKU relation: {relation.kind}")

    if base_model_in_text(model, primary) or match == "exact":
        return PageIdentityAssessment("exact", primary, "Main product names the requested model")
    return PageIdentityAssessment("unknown", primary, "Main product does not establish the requested identity")


def _same(left: str | None, right: str | None) -> bool:
    return bool(left and right) and normalized_identity(left) == normalized_identity(right)


@dataclass(frozen=True, slots=True)
class IdentityEvidence:
    """A typed identity fact supplied by a source rather than guessed from syntax."""

    field: Literal[
        "brand", "base_model", "commercial_model", "manufacturer_article", "product_code", "sku",
        "gtin", "color", "market_hint", "market_scope",
    ]
    value: str
    source: str | None = None

    def __post_init__(self) -> None:
        if self.field not in IDENTITY_EVIDENCE_FIELDS:
            raise ValueError(f"unsupported identity evidence field: {self.field}")
        value = _clean(self.value)
        if not value:
            raise ValueError("identity evidence value must not be empty")
        if self.field == "market_scope" and value not in MARKET_SCOPES:
            raise ValueError(f"unsupported market_scope evidence: {value}")
        object.__setattr__(self, "value", value)
        object.__setattr__(self, "source", _clean(self.source))


@dataclass(frozen=True, slots=True)
class ProductIdentity:
    """Resolved product identity, retaining uncertainty and unresolved codes."""

    brand: str
    raw_name: str
    base_model: str | None = None
    commercial_model: str | None = None
    manufacturer_article: str | None = None
    product_code: str | None = None
    sku: str | None = None
    gtin: str | None = None
    color: str | None = None
    configuration: dict[str, str] = field(default_factory=dict)
    variant_suffix: str | None = None
    market_hint: str | None = None
    market_scope: MarketScope = "unknown"
    confidence: IdentityConfidence = "low"
    evidence: list[str] = field(default_factory=list)
    candidate_identifiers: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        brand = _clean(self.brand)
        raw_name = _clean(self.raw_name)
        if not brand or not raw_name:
            raise ValueError("brand and raw_name are required")
        if self.market_scope not in MARKET_SCOPES:
            raise ValueError(f"unsupported market_scope: {self.market_scope}")
        if self.confidence not in CONFIDENCE_LEVELS:
            raise ValueError(f"unsupported confidence: {self.confidence}")
        object.__setattr__(self, "brand", brand)
        object.__setattr__(self, "raw_name", raw_name)
        for name in (
            "base_model", "commercial_model", "manufacturer_article", "product_code", "sku", "gtin",
            "color", "variant_suffix", "market_hint",
        ):
            object.__setattr__(self, name, _clean(getattr(self, name)))
        object.__setattr__(self, "configuration", {
            str(key): str(value) for key, value in self.configuration.items()
            if _clean(str(key)) and _clean(str(value))
        })
        object.__setattr__(self, "evidence", list(dict.fromkeys(self.evidence)))
        object.__setattr__(
            self, "candidate_identifiers",
            list(dict.fromkeys(value for value in map(_clean, self.candidate_identifiers) if value)),
        )


@dataclass(frozen=True, slots=True)
class IdentityComparison:
    relation: IdentityRelation
    evidence: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.relation not in RELATIONS:
            raise ValueError(f"unsupported identity relation: {self.relation}")


@dataclass(frozen=True, slots=True)
class AttributeScopeDecision:
    """Future-facing scope annotation; the resolver leaves it unknown without evidence."""

    attribute_scope: AttributeScope = "unknown"
    evidence: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.attribute_scope not in ATTRIBUTE_SCOPES:
            raise ValueError(f"unsupported attribute_scope: {self.attribute_scope}")


def _coerce_evidence(
    items: Iterable[IdentityEvidence | Mapping[str, str]] | None,
) -> list[IdentityEvidence]:
    coerced: list[IdentityEvidence] = []
    for item in items or ():
        if isinstance(item, IdentityEvidence):
            coerced.append(item)
        else:
            coerced.append(IdentityEvidence(
                field=item["field"],  # type: ignore[arg-type]
                value=item["value"],
                source=item.get("source"),
            ))
    return coerced


def _field_values(items: Iterable[IdentityEvidence]) -> dict[str, IdentityEvidence]:
    values: dict[str, IdentityEvidence] = {}
    for item in items:
        values.setdefault(item.field, item)
    return values


def _remove_once(text: str, value: str) -> str:
    pattern = re.compile(rf"(?<!\w){re.escape(value)}(?!\w)", re.IGNORECASE)
    return pattern.sub(" ", text, count=1)


def _extract_configuration(text: str) -> tuple[dict[str, str], str, str | None]:
    for pattern in (SPACED_CONFIGURATION_RE, CONFIGURATION_RE):
        match = pattern.search(text)
        if not match:
            continue
        ram = int(match.group("ram"))
        storage = int(match.group("storage"))
        if ram not in PLAUSIBLE_RAM or storage not in PLAUSIBLE_STORAGE or storage <= ram:
            continue
        configuration = {"ram": f"{ram} GB", "storage": f"{storage} GB"}
        remaining = f"{text[:match.start()]} {text[match.end():]}"
        return configuration, remaining, match.group(0)
    return {}, text, None


def _extract_color(text: str) -> tuple[str | None, str]:
    tokens = list(re.finditer(r"(?<!\w)[A-Za-z]+(?!\w)", text))
    for match in reversed(tokens):
        if match.group(0).casefold() in COLOR_NAMES:
            remaining = f"{text[:match.start()]} {text[match.end():]}"
            return match.group(0), remaining
    return None, text


def _looks_like_unresolved_code(token: str) -> bool:
    compact = re.sub(r"[^A-Za-z0-9]", "", token)
    return (
        len(compact) >= 7
        and sum(character.isalpha() for character in compact) >= 2
        and sum(character.isdigit() for character in compact) >= 2
        and compact.upper() == compact
    )


def _extract_trailing_code(text: str) -> tuple[str | None, str]:
    tokens = text.split()
    if len(tokens) < 2 or not _looks_like_unresolved_code(tokens[-1]):
        return None, text
    return tokens[-1], " ".join(tokens[:-1])


def _split_suffix(model: str | None) -> tuple[str | None, str | None]:
    model = _clean(model)
    if not model:
        return None, None
    slash = re.fullmatch(r"(.+?)/([A-Za-z0-9]{1,4})", model)
    if slash and any(character.isdigit() for character in slash.group(1)):
        return _clean(slash.group(1)), slash.group(2)
    hyphen = re.fullmatch(r"(.+?)-([A-Za-z0-9]{1,3})", model)
    if (
        hyphen and len(normalized_identity(hyphen.group(1))) >= 6
        and any(character.isdigit() for character in hyphen.group(1))
    ):
        return _clean(hyphen.group(1)), hyphen.group(2)
    return model, None


def resolve_product_identity(
    raw_name: str,
    brand: str | None = None,
    evidence: Iterable[IdentityEvidence | Mapping[str, str]] | None = None,
) -> ProductIdentity:
    """Parse a user-facing product name without assigning unsupported semantics."""
    raw = _clean(raw_name)
    if not raw:
        raise ValueError("raw_name is required")
    supplied = _coerce_evidence(evidence)
    facts = _field_values(supplied)

    resolved_brand = _clean(brand) or (facts.get("brand").value if facts.get("brand") else None)
    remaining = raw
    if resolved_brand:
        remaining = _remove_once(remaining, resolved_brand)
    else:
        tokens = remaining.split()
        if len(tokens) < 2 or not re.search(r"[A-Za-z]", tokens[0]):
            raise ValueError("brand is required when it cannot be separated from raw_name")
        resolved_brand = tokens[0]
        remaining = " ".join(tokens[1:])

    notes: list[str] = [f"Brand separated from input: {resolved_brand}."]
    configuration, remaining, configuration_raw = _extract_configuration(remaining)
    if configuration_raw:
        notes.append(f"Explicit capacity pair parsed as RAM/storage: {configuration_raw}.")

    parsed_color, remaining = _extract_color(remaining)
    if parsed_color:
        notes.append(f"Recognized standalone color token: {parsed_color}.")

    for fact in supplied:
        if fact.field in {
            "commercial_model", "base_model", "manufacturer_article", "product_code", "sku", "gtin", "color",
        }:
            remaining = _remove_once(remaining, fact.value)
        source = f" ({fact.source})" if fact.source else ""
        notes.append(f"Evidence {fact.field}={fact.value}{source}.")

    candidate_code, remaining_without_code = _extract_trailing_code(remaining)
    candidate_identifiers = [candidate_code] if candidate_code else []
    if candidate_code:
        remaining = remaining_without_code
        notes.append(
            f"Unresolved identifier retained without assigning model/SKU semantics: {candidate_code}."
        )

    model_from_text = _clean(remaining.strip(" ,-"))
    evidence_model = facts.get("base_model") or facts.get("commercial_model")
    model_value = evidence_model.value if evidence_model else model_from_text
    base_model, variant_suffix = _split_suffix(model_value)
    if variant_suffix:
        notes.append(f"Delimited variant suffix retained without regional interpretation: {variant_suffix}.")

    commercial_model = facts.get("commercial_model").value if facts.get("commercial_model") else base_model

    market_scope: MarketScope = "unknown"
    if facts.get("market_scope") and facts["market_scope"].value in MARKET_SCOPES:
        market_scope = facts["market_scope"].value  # type: ignore[assignment]

    proven_fields = sum(
        facts.get(name) is not None
        for name in ("base_model", "commercial_model", "manufacturer_article", "product_code", "sku", "gtin")
    )
    confidence: IdentityConfidence
    if base_model and (proven_fields or (not candidate_identifiers and resolved_brand)):
        confidence = "high"
    elif base_model:
        confidence = "medium"
    else:
        confidence = "low"

    return ProductIdentity(
        brand=resolved_brand,
        raw_name=raw,
        base_model=base_model,
        commercial_model=commercial_model,
        manufacturer_article=facts.get("manufacturer_article").value if facts.get("manufacturer_article") else None,
        product_code=facts.get("product_code").value if facts.get("product_code") else None,
        sku=facts.get("sku").value if facts.get("sku") else None,
        gtin=facts.get("gtin").value if facts.get("gtin") else None,
        color=facts.get("color").value if facts.get("color") else parsed_color,
        configuration=configuration,
        variant_suffix=variant_suffix,
        market_hint=facts.get("market_hint").value if facts.get("market_hint") else None,
        market_scope=market_scope,
        confidence=confidence,
        evidence=notes,
        candidate_identifiers=candidate_identifiers,
    )


def compare_identities(left: ProductIdentity, right: ProductIdentity) -> IdentityComparison:
    """Compare identities using explicit model signals and conservative conflicts."""
    evidence: list[str] = []
    conflicts: list[str] = []
    if not _same(left.brand, right.brand):
        conflicts.append(f"brand differs: {left.brand!r} vs {right.brand!r}")
        return IdentityComparison("different_model", evidence, conflicts)
    if not left.base_model or not right.base_model:
        return IdentityComparison("unknown", evidence, ["base model is missing"])
    if not _same(left.base_model, right.base_model):
        conflicts.append(f"base model differs: {left.base_model!r} vs {right.base_model!r}")
        return IdentityComparison("different_model", evidence, conflicts)

    evidence.append(f"equivalent normalized base model: {left.base_model!r} / {right.base_model!r}")
    shared_specific = False
    for field_name in ("variant_suffix", "color", "manufacturer_article", "product_code", "sku", "gtin"):
        left_value = getattr(left, field_name)
        right_value = getattr(right, field_name)
        if left_value and right_value:
            if _same(left_value, right_value):
                evidence.append(f"matching {field_name}: {left_value}")
                shared_specific = True
            else:
                conflicts.append(f"{field_name} differs: {left_value!r} vs {right_value!r}")
    for key in set(left.configuration) & set(right.configuration):
        if _same(left.configuration[key], right.configuration[key]):
            evidence.append(f"matching configuration {key}: {left.configuration[key]}")
            shared_specific = True
        else:
            conflicts.append(
                f"configuration {key} differs: {left.configuration[key]!r} vs {right.configuration[key]!r}"
            )

    if shared_specific and not conflicts:
        return IdentityComparison("exact_variant", evidence, conflicts)
    return IdentityComparison("same_base_model", evidence, conflicts)


def compare_identity_names(
    left: str,
    right: str,
    brand: str | None = None,
) -> IdentityComparison:
    """Convenience wrapper for comparing two user-facing names."""
    return compare_identities(
        resolve_product_identity(left, brand=brand),
        resolve_product_identity(right, brand=brand),
    )


def identity_verification_signals(identity: ProductIdentity) -> dict[str, str]:
    """Return optional identity signals Discovery may verify independently."""
    signals = dict(identity.configuration)
    for name in (
        "color", "manufacturer_article", "product_code", "sku", "gtin", "variant_suffix", "market_hint",
    ):
        value = getattr(identity, name)
        if value:
            signals[name] = value
    for index, value in enumerate(identity.candidate_identifiers, 1):
        signals[f"unresolved_identifier_{index}"] = value
    return signals
