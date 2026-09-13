"""Compact, conservative product category detection."""

from __future__ import annotations

from dataclasses import dataclass, field
import re
import unicodedata
from typing import Iterable, Literal, Protocol

from core.identity import ProductIdentity


CategoryConfidence = Literal["high", "medium", "low"]
CategorySource = Literal["identity", "extracted_attributes", "mixed", "unknown"]

CATEGORY_NAMES = {
    "unknown": ("Unknown", None),
    "cooktop": ("Cooktop", "major_appliance"),
    "smartphone": ("Smartphone", "consumer_electronics"),
    "sewing_machine": ("Sewing machine", "sewing"),
    "air_fryer": ("Air fryer", "small_appliance"),
    "wet_dry_vacuum": ("Wet/dry vacuum", "home_cleaning"),
}


class AttributeLike(Protocol):
    name: str
    value: str
    evidence: str


@dataclass(frozen=True, slots=True)
class CategoryResult:
    category_id: str
    category_name: str
    parent_category: str | None
    confidence: CategoryConfidence
    evidence: list[str] = field(default_factory=list)
    source: CategorySource = "unknown"

    def __post_init__(self) -> None:
        if self.category_id not in CATEGORY_NAMES:
            raise ValueError(f"unsupported category_id: {self.category_id}")
        if self.confidence not in {"high", "medium", "low"}:
            raise ValueError(f"unsupported confidence: {self.confidence}")
        if self.source not in {"identity", "extracted_attributes", "mixed", "unknown"}:
            raise ValueError(f"unsupported category source: {self.source}")


@dataclass(frozen=True, slots=True)
class _Signal:
    pattern: re.Pattern[str]
    weight: int
    description: str


def _pattern(*phrases: str) -> re.Pattern[str]:
    alternatives = "|".join(re.escape(phrase) for phrase in phrases)
    return re.compile(rf"(?<!\w)(?:{alternatives})(?!\w)", re.IGNORECASE)


TEXT_SIGNALS: dict[str, tuple[_Signal, ...]] = {
    "cooktop": (
        _Signal(_pattern("cooktop", "induction hob", "electric hob", "варочная панель",
                         "индукционная панель"), 7, "explicit cooktop product phrase"),
    ),
    "smartphone": (
        _Signal(_pattern("smartphone", "smart phone", "mobile phone", "смартфон"),
                7, "explicit smartphone product phrase"),
    ),
    "sewing_machine": (
        _Signal(_pattern("sewing machine", "швейная машина", "швейную машину", "швейную машинку"),
                7, "explicit sewing-machine product phrase"),
    ),
    "air_fryer": (
        _Signal(_pattern("air fryer", "airfryer", "аэрогриль", "аэрогриля"),
                7, "explicit air-fryer product phrase"),
    ),
    "wet_dry_vacuum": (
        _Signal(_pattern("wet dry vacuum", "wet/dry vacuum", "wet & dry vacuum",
                         "wet and dry vacuum", "моющий пылесос", "floor washer"),
                7, "explicit wet/dry vacuum product phrase"),
        _Signal(_pattern("wet & dry", "wet and dry"), 4, "wet/dry product phrase"),
    ),
}


ATTRIBUTE_SIGNALS: dict[str, tuple[_Signal, ...]] = {
    "cooktop": (
        _Signal(_pattern("number of zones", "cooking zones", "connection rating",
                         "connected load", "installation dimensions", "heating type",
                         "количество конфорок", "мощность подключения"),
                2, "cooktop attribute"),
    ),
    "smartphone": (
        _Signal(_pattern("cpu model", "processor", "gpu", "ram", "internal storage",
                         "rear camera", "front camera", "sim card", "display resolution",
                         "battery capacity"), 2, "smartphone attribute"),
    ),
    "sewing_machine": (
        _Signal(_pattern("machine type", "shuttle type", "operation count", "buttonhole type",
                         "stitch length", "stitch width", "presser foot lift", "тип машины",
                         "тип челнока", "количество операций", "выполнение петли",
                         "длина стежка", "ширина стежка"), 2, "sewing-machine attribute"),
    ),
    "air_fryer": (
        _Signal(_pattern("number of bowls", "bowl capacity", "program count",
                         "temperature range", "timer range", "количество чаш",
                         "объем каждой чаши", "количество программ", "температура",
                         "таймер"), 2, "air-fryer attribute"),
    ),
    "wet_dry_vacuum": (
        _Signal(_pattern("suction power", "clean water tank", "dirty water tank",
                         "self cleaning", "self-cleaning", "charging time",
                         "всасывания", "бак для чистой воды", "бак для грязной воды",
                         "самоочистка", "შესრუტვის სიმძლავრე",
                         "სუფთა წყლის კონტეინერის მოცულობა",
                         "ჭუჭყიანი წყლის კონტეინერის მოცულობა"),
                2, "wet/dry vacuum attribute"),
    ),
}


def _normalize(value: str | None) -> str:
    return " ".join(unicodedata.normalize("NFKC", value or "").split())


def _unknown(evidence: list[str] | None = None) -> CategoryResult:
    name, parent = CATEGORY_NAMES["unknown"]
    return CategoryResult("unknown", name, parent, "low", evidence or [], "unknown")


def detect_category(
    identity: ProductIdentity | None = None,
    attributes: Iterable[AttributeLike] = (),
    product_texts: Iterable[str] = (),
) -> CategoryResult:
    """Detect a compact schema category from product facts, or return unknown."""
    identity_texts: list[str] = []
    if identity is not None:
        identity_texts.extend((identity.raw_name, identity.commercial_model or ""))
    identity_texts.extend(text for text in product_texts if text)

    extracted = list(attributes)
    attribute_texts = [
        " ".join(filter(None, (
            _normalize(getattr(item, "name", "")),
            _normalize(getattr(item, "value", "")),
            _normalize(getattr(item, "evidence", "")),
        )))
        for item in extracted
    ]
    structured_type_texts = [
        _normalize(getattr(item, "value", ""))
        for item in extracted
        if _normalize(getattr(item, "name", "")).casefold() in {
            "category", "product category", "product type", "категория", "тип товара",
        }
    ]

    scores = {category: 0 for category in CATEGORY_NAMES if category != "unknown"}
    evidence: dict[str, list[str]] = {category: [] for category in scores}
    identity_hits: set[str] = set()
    attribute_hits: set[str] = set()

    identity_haystack = " | ".join(identity_texts)
    for category, signals in TEXT_SIGNALS.items():
        for signal in signals:
            match = signal.pattern.search(identity_haystack)
            if match:
                scores[category] += signal.weight
                identity_hits.add(category)
                evidence[category].append(
                    f"Identity/title contains {signal.description}: {match.group(0)!r}."
                )
        for text in structured_type_texts:
            for signal in signals:
                match = signal.pattern.search(text)
                if match:
                    scores[category] += signal.weight
                    attribute_hits.add(category)
                    evidence[category].append(
                        f"Structured product type contains {signal.description}: {match.group(0)!r}."
                    )

    for category, signals in ATTRIBUTE_SIGNALS.items():
        matched_names: set[str] = set()
        for text in attribute_texts:
            for signal in signals:
                match = signal.pattern.search(text)
                if not match:
                    continue
                key = match.group(0).casefold()
                if key in matched_names:
                    continue
                matched_names.add(key)
                scores[category] += signal.weight
                attribute_hits.add(category)
                evidence[category].append(
                    f"Extracted attributes contain {signal.description}: {match.group(0)!r}."
                )

    ranked = sorted(scores, key=lambda category: scores[category], reverse=True)
    winner = ranked[0]
    winner_score = scores[winner]
    runner_up = scores[ranked[1]] if len(ranked) > 1 else 0
    if winner_score < 4 or winner_score == runner_up:
        return _unknown(["No category has sufficient unambiguous product evidence."])

    if winner_score >= 7 and winner_score - runner_up >= 3:
        confidence: CategoryConfidence = "high"
    elif winner_score >= 4 and winner_score - runner_up >= 2:
        confidence = "medium"
    else:
        confidence = "low"

    from_identity = winner in identity_hits
    from_attributes = winner in attribute_hits
    source: CategorySource
    if from_identity and from_attributes:
        source = "mixed"
    elif from_identity:
        source = "identity"
    elif from_attributes:
        source = "extracted_attributes"
    else:
        source = "unknown"
    name, parent = CATEGORY_NAMES[winner]
    return CategoryResult(winner, name, parent, confidence, evidence[winner], source)
