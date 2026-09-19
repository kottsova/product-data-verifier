"""Generic SKU structure: base model, regional/market suffix, real variant.

Manufacturers publish one product under several identifiers.  Some differ only
by a market code (``DCD796P2`` vs ``DCD796P2-GB``, ``HX9992/12`` vs
``HX9992_12``); others name a genuinely different product (``DCD796P2`` vs
``DCD796P2T`` or ``DCD796D2``).  This module separates the three without any
brand or product table:

* ``base model``            -- the distinctive alphanumeric identifier;
* ``regional SKU suffix``   -- a short delimiter-separated market/bundle code;
* ``real product variant``  -- anything else that changes the identifier.

Delimiters ``/``, ``_`` and ``-`` are interchangeable in URLs and titles, so
``HX9992/12`` and ``HX9992_12`` are the same identifier.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Literal

SkuRelationKind = Literal[
    "exact",             # base and (when requested) suffix agree
    "regional_suffix",   # requested bare base; candidate adds a market code
    "base_only",         # requested base+suffix; candidate omits the suffix
    "different_suffix",  # same base, a different suffix than requested
    "different_variant", # same base extended by a non-regional variant tag
    "absent",            # requested identifier does not occur
]

_TOKEN_RE = re.compile(r"[A-Za-z0-9]+(?:[/_\-.][A-Za-z0-9]+)*")
_SPLIT_RE = re.compile(r"[/_\-.]")
# Two-letter tags that are commercial-family modifiers, never market codes.
_NON_REGIONAL_TWO_LETTER = {"FE", "SE", "XL", "XS", "XR", "LE", "GT", "GS", "OS", "HD", "UV", "TV"}


def _compact(value: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", value.upper())


def _is_mixed(token: str) -> bool:
    compact = _compact(token)
    return (
        len(compact) >= 4
        and any(ch.isalpha() for ch in compact)
        and any(ch.isdigit() for ch in compact)
    )


def classify_sku_suffix(suffix: str) -> Literal["regional", "variant"]:
    """Classify a delimiter-separated tail of a SKU."""
    tail = _compact(suffix)
    if not tail:
        return "variant"
    if re.fullmatch(r"[A-Z]{2}", tail) and tail not in _NON_REGIONAL_TWO_LETTER:
        return "regional"
    if re.fullmatch(r"[A-Z][0-9]|[0-9][A-Z]", tail):
        return "regional"
    if re.fullmatch(r"[0-9]{1,2}", tail):
        return "regional"
    return "variant"


@dataclass(frozen=True, slots=True)
class RequestedSku:
    """The distinctive identifier extracted from a requested model string."""

    raw: str
    base: str          # compact base identifier
    suffix: str        # compact requested suffix, "" when none
    base_display: str  # base as written (delimiters preserved)

    @property
    def full(self) -> str:
        return f"{self.base}{self.suffix}"


def requested_sku(model: str | None) -> RequestedSku | None:
    """Pick the most distinctive identifier of a (possibly multi-word) model.

    ``"Sonicare 9900 Prestige HX9992/12"`` -> base ``HX9992`` suffix ``12``;
    ``"GAF-1825"`` -> base ``GAF1825``; ``"DCD796P2-GB"`` -> ``DCD796P2`` + ``GB``.
    Returns ``None`` when the model has no letter+digit identifier.
    """
    best: tuple[int, str] | None = None
    for token in re.findall(r"\S+", model or ""):
        cleaned = token.strip(",;:()[]\"'")
        if not _is_mixed(cleaned) and not (
            re.fullmatch(r"[A-Za-z]{2,5}[-_/ ]?\d{3,}", cleaned) is not None
        ):
            continue
        length = len(_compact(cleaned))
        if best is None or length > best[0]:
            best = (length, cleaned)
    if best is None:
        return None
    raw = best[1]
    parts = [part for part in _SPLIT_RE.split(raw) if part]
    if len(parts) >= 2:
        head, tail = "-".join(parts[:-1]), parts[-1]
        delimiter_is_slash = "/" in raw or "_" in raw
        tail_kind = classify_sku_suffix(tail)
        head_is_identifier = _is_mixed(head) and len(_compact(head)) >= 5
        if head_is_identifier and (
            delimiter_is_slash
            or (tail_kind == "regional" and not _compact(tail).isdigit())
            or (tail_kind == "regional" and len(_compact(tail)) <= 2)
        ):
            return RequestedSku(raw, _compact(head), _compact(tail), head)
    return RequestedSku(raw, _compact(raw), "", raw)


def sku_search_terms(model: str | None) -> list[str]:
    """Query strings a manufacturer's own search box is likely to accept."""
    sku = requested_sku(model)
    terms: list[str] = []
    if sku is not None:
        if sku.suffix:
            terms.append(f"{sku.base_display}/{sku.suffix}")
        terms.append(sku.base_display)
    cleaned = " ".join((model or "").split())
    if cleaned:
        terms.append(cleaned)
    return list(dict.fromkeys(term for term in terms if term))


@dataclass(frozen=True, slots=True)
class SkuRelation:
    kind: SkuRelationKind
    suffix: str = ""        # candidate's suffix (compact) when it has one
    evidence: str = ""      # the token that produced the relation

    @property
    def same_model(self) -> bool:
        return self.kind in {"exact", "regional_suffix"}


_ORDER: dict[str, int] = {
    "exact": 0, "regional_suffix": 1, "base_only": 2,
    "different_suffix": 3, "different_variant": 4, "absent": 5,
}


def sku_relation(model: str | None, *texts: str | None) -> SkuRelation:
    """Relate every identifier-looking token in ``texts`` to the requested SKU."""
    sku = requested_sku(model)
    if sku is None:
        return SkuRelation("absent")
    best = SkuRelation("absent")
    for text in texts:
        for token in _TOKEN_RE.findall(text or ""):
            parts = [part for part in _SPLIT_RE.split(token) if part]
            for start in range(len(parts)):
                matched = False
                for cut in range(start + 1, len(parts) + 1):
                    if _compact("".join(parts[start:cut])) != sku.base:
                        continue
                    relation = _relate_tail(sku, parts[cut:], token)
                    if _ORDER[relation.kind] < _ORDER[best.kind]:
                        best = relation
                    matched = True
                    break
                if matched:
                    continue
                # A longer single identifier that merely starts with the base
                # (DCD796P2T for DCD796P2) is a real variant, not a market code.
                head = _compact(parts[start])
                if (
                    head.startswith(sku.base)
                    and head != sku.base
                    and _ORDER["different_variant"] < _ORDER[best.kind]
                ):
                    best = SkuRelation("different_variant", head[len(sku.base):], token)
    return best


def _is_slug_word(part: str) -> bool:
    """A continuation of a URL slug/title rather than a SKU tag."""
    return (part.isalpha() and len(part) >= 4) or (part.isdigit() and len(part) > 2)


def _relate_tail(sku: RequestedSku, tail: list[str], token: str) -> SkuRelation:
    if not tail:
        return SkuRelation("exact" if not sku.suffix else "base_only", "", token)
    first = _compact(tail[0])
    if sku.suffix:
        if first == sku.suffix:
            return SkuRelation("exact", first, token)
        if _is_slug_word(first):
            return SkuRelation("base_only", "", token)
        return SkuRelation("different_suffix", first, token)
    if classify_sku_suffix(first) == "regional":
        return SkuRelation("regional_suffix", first, token)
    if _is_slug_word(first):
        return SkuRelation("exact", "", token)
    return SkuRelation("different_variant", first, token)


def sku_in_text_loosely(model: str | None, text: str | None) -> bool:
    """Candidate-gathering check: does the text carry the requested identifier?

    Deliberately permissive about suffixes (``dcd796p2-gb`` qualifies); final
    acceptance still goes through :func:`sku_relation` and the relevance gate.
    """
    sku = requested_sku(model)
    if sku is None:
        return False
    compact_text = _compact(text or "")
    return (sku.base + sku.suffix) in compact_text or (
        not sku.suffix and sku.base in compact_text
    )
