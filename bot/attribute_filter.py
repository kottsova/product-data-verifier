"""User-facing attribute filtering for the Telegram message and /export CSV.

Extraction keeps every raw fact it finds (see core.schema's
``extend_schema_with_discovered`` -- "never discard them"), including
regulatory boilerplate, help-center instructions, and other page furniture
that happens to sit next to real specs on a support/legal page. That is the
right call for the internal VerifyProductResult (diagnostics need it), but
it is wrong to hand straight to a shopper.

This module is presentation-only: it never touches services.product_verifier
data, only decides which already-computed ServiceAttribute rows are shown.
Only bot.formatters and bot.export import it, so the stable service contract
and the persisted cache are untouched -- rerunning a cached result with a
newer filter changes what's displayed without re-verifying anything.
"""

from __future__ import annotations

import re
from typing import Iterable

from services.product_verifier import ServiceAttribute


# A raw label that means roughly the same thing as an already-present
# canonical field, but couldn't be safely folded into it (see core.mapping's
# value-shape guards for "battery" vs "battery_capacity" and "cpu" vs
# "processor"). Showing it as its own row reads as a confusing duplicate.
DISCOVERED_SYNONYM_SUPPRESS = {
    "battery": "battery_capacity",
    "chipset": "processor",
    "cpu": "processor",
    "cpu_model": "processor",
    "memory": "ram",
    "internal_memory": "storage",
}

# discovered=True fields are unvetted extraction leftovers -- held to a
# tighter noise bar than canonical/expected schema fields, which deserve
# more leeway for legitimately verbose specs (e.g. a long ports/features
# list) and are already validated by the mapping layer.
_DISCOVERED_MAX_LABEL_WORDS = 5
_DISCOVERED_MAX_VALUE_WORDS = 8
_DISCOVERED_MAX_VALUE_CHARS = 70
_CANONICAL_MAX_VALUE_WORDS = 20
_CANONICAL_MAX_VALUE_CHARS = 200

_RF_UNIT_RE = re.compile(r"dbm|dbua|dbµa", re.IGNORECASE)
# Non-Latin, non-Cyrillic scripts (CJK/Hangul/Kana and similar): a value or
# label written in a script our canonical schema and UI never use is either
# mojibake or page furniture the pipeline couldn't translate -- either way,
# not a usable characteristic to show as-is.
_FOREIGN_SCRIPT_RE = re.compile(
    r"[぀-ヿ㐀-䶿一-鿿가-힣豈-﫿]"
)


def _looks_foreign(text: str) -> bool:
    return bool(_FOREIGN_SCRIPT_RE.search(text))


def _value_text(value: object) -> str | None:
    """Plain text to noise-check, or None for a shape (e.g. dimensions) that isn't prose."""
    if value is None:
        return ""
    if isinstance(value, dict):
        return None
    return str(value)


def _is_noisy(label: str, value: object, *, discovered: bool) -> bool:
    text = _value_text(value)
    if _looks_foreign(label) or (text is not None and _looks_foreign(text)):
        return True
    if text is not None and _RF_UNIT_RE.search(text):
        return True
    if discovered:
        if len(label.split()) > _DISCOVERED_MAX_LABEL_WORDS:
            return True
        if text is not None and (
            len(text) > _DISCOVERED_MAX_VALUE_CHARS
            or len(text.split()) > _DISCOVERED_MAX_VALUE_WORDS
        ):
            return True
    elif text is not None and (
        len(text) > _CANONICAL_MAX_VALUE_CHARS
        or len(text.split()) > _CANONICAL_MAX_VALUE_WORDS
    ):
        return True
    return False


def is_user_facing(attribute: ServiceAttribute) -> bool:
    """Whether ``attribute`` belongs in a user-facing message/export row."""
    if _is_noisy(attribute.display_name, attribute.value, discovered=attribute.discovered):
        return False
    if attribute.discovered and attribute.canonical_name in DISCOVERED_SYNONYM_SUPPRESS:
        return False
    return True


def filter_user_facing(attributes: Iterable[ServiceAttribute]) -> list[ServiceAttribute]:
    """Every attribute worth showing a shopper, in its original order."""
    return [item for item in attributes if is_user_facing(item)]
