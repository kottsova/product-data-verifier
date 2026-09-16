"""Deterministic, framework-free parsing of a user's product query message.

No Telegram types here on purpose: this module is plain text in, a typed
result out, so it can be unit tested without any bot framework or network.
"""

from __future__ import annotations

from dataclasses import dataclass


_CONVERSATIONAL_WORDS = frozenset({
    "мне",
    "помоги",
    "такое",
    "что",
    "это",
})
_CONVERSATIONAL_PUNCTUATION = ".,!?…:;"


@dataclass(frozen=True, slots=True)
class ParsedProductQuery:
    brand: str
    model: str
    article: str | None = None


def _is_obviously_conversational(tokens: list[str]) -> bool:
    """Recognize a very small set of clearly conversational plain inputs.

    Requiring every token to be a common conversational word keeps this guard
    conservative: it does not reject Cyrillic or Unicode product names, and
    any product-like token is enough to leave the existing parser behavior
    unchanged.
    """
    normalized = [
        token.casefold().strip(_CONVERSATIONAL_PUNCTUATION)
        for token in tokens
    ]
    return 2 <= len(normalized) <= 4 and all(
        token and token in _CONVERSATIONAL_WORDS for token in normalized
    )


def parse_product_query(text: str) -> ParsedProductQuery | None:
    """Parse "Brand Model" or "Brand | Model[ | Article]". None if ambiguous.

    Two deterministic formats are supported:
      - Pipe-delimited: "ExampleCo | Model 200" or
        "ExampleCo | Model 200 | ART-7".
        Exactly 2 or 3 non-empty, trimmed parts are required.
      - Plain whitespace: "ExampleCo Model 200" -- the first token is the brand,
        everything after it (whitespace-collapsed) is the model. This mirrors
        how core.identity.resolve_product_identity separates brand from a
        raw product name, so a Telegram query and a CLI ``brand model`` call
        resolve identity the same way.

    Anything that doesn't cleanly fit one of these two shapes returns None
    rather than guessing, so the caller can ask the user to rephrase instead
    of running a workflow against a misparsed brand/model.
    """
    cleaned = " ".join((text or "").strip().split())
    if not cleaned:
        return None

    if "|" in text:
        parts = [part.strip() for part in text.split("|")]
        if len(parts) not in (2, 3) or any(not part for part in parts):
            return None
        brand, model, *rest = parts
        article = rest[0] if rest else None
        return ParsedProductQuery(brand=brand, model=model, article=article)

    tokens = cleaned.split(" ")
    if len(tokens) < 2:
        return None
    if _is_obviously_conversational(tokens):
        return None
    brand = tokens[0]
    model = " ".join(tokens[1:])
    return ParsedProductQuery(brand=brand, model=model)
