"""Shared, conservative normalization helpers for product data labels."""

from __future__ import annotations

import re
import unicodedata


_UNIT_SUFFIX = (
    r"(?:°\s*[cf]|[кk]?[вw]т|[вv]|гц|hz|mah|ач|ah|мл|ml|л|l|"
    r"кг|kg|гр?|g|мм|mm|см|cm|м|m|мин|сек|ч|шт|pcs?|дб|db|"
    r"(?:[швгдт]\s*[xх×]\s*){2}[швгдт]|(?:[hwdlt]\s*[xх×]\s*){2}[hwdlt])"
)
_BRACKETED_UNIT_SUFFIX_RE = re.compile(
    rf"\s*[\(\[]\s*{_UNIT_SUFFIX}\s*[\)\]]\s*$",
    re.IGNORECASE,
)
_DELIMITED_UNIT_SUFFIX_RE = re.compile(
    rf"\s*[,;:]\s*{_UNIT_SUFFIX}\s*$",
    re.IGNORECASE,
)
_BARE_UNIT_SUFFIX_RE = re.compile(rf"\s+{_UNIT_SUFFIX}\s*$", re.IGNORECASE)
_NUMBERED_LABEL_RE = re.compile(r"\s+(?:(?:no|n)\s*)?\d+\s*$", re.IGNORECASE)


def normalize_attribute_label(value: str | None) -> str:
    """Return a stable label key without guessing the label's semantics.

    NFKC handles compatibility punctuation, Russian ``ё`` is folded to ``е``,
    and only an explicitly delimited, known unit suffix is discarded.  Units
    in values and meaningful words in labels remain untouched.
    """

    text = unicodedata.normalize("NFKC", value or "").casefold().replace("ё", "е")
    # A label may stack a bracketed dimension legend and a trailing bare unit
    # ("Размеры (ШxВxТ) мм"), so strip trailing unit/legend noise repeatedly
    # until nothing more comes off, not just once.
    for _ in range(4):
        stripped = _BRACKETED_UNIT_SUFFIX_RE.sub("", text)
        stripped = _DELIMITED_UNIT_SUFFIX_RE.sub("", stripped)
        stripped = _BARE_UNIT_SUFFIX_RE.sub("", stripped)
        if stripped == text:
            break
        text = stripped
    return " ".join(re.findall(r"[\w]+", text))


def attribute_label_variants(value: str | None) -> tuple[str, ...]:
    """Return conservative lookup variants for a raw attribute label.

    Slash/pipe-separated synonyms are considered independently, while a
    trailing numeric discriminator (for example ``Bowl capacity 1``) may fall
    back to an already-defined base alias.  Callers still require all matching
    variants to resolve to one canonical field.
    """

    raw = unicodedata.normalize("NFKC", value or "")
    candidates = [raw]
    if re.search(r"\s[/|]\s", raw):
        candidates.extend(re.split(r"\s[/|]\s", raw))

    keys: list[str] = []
    for candidate in candidates:
        key = normalize_attribute_label(candidate)
        if key and key not in keys:
            keys.append(key)
        base = _NUMBERED_LABEL_RE.sub("", key)
        if base and base != key and base not in keys:
            keys.append(base)
    return tuple(keys)
