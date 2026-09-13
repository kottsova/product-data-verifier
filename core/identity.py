"""Conservative parsing and comparison of product identity strings."""

from __future__ import annotations

from dataclasses import dataclass, field
import re
import unicodedata
from typing import Iterable, Literal, Mapping


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
