"""User-facing attribute filtering for the Telegram message and /export CSV.

Stage 31.1 first tried a noise-heuristic blacklist (foreign script, RF-unit
patterns, oversized values) on top of every attribute the pipeline produced.
A real Pixel 9 Pro retest showed that approach is fundamentally leaky: a
support/help page yields dozens of short, plausible-looking English
fragments ("Pixel", "Features", "Video", "Google Llc", "True", "Cja",
"Anonymous", "Max", ...) that no length/script heuristic catches, because
they're not identifiable as noise by their *shape* -- only by the fact that
they were never part of the category's canonical schema to begin with.

So this module is now a strict allowlist: a user-facing row is exactly the
identity fields plus whatever the category schema defines (core.schema's
non-"discovered" attributes; see core.profile's discovered flag). Raw
extraction leftovers (``discovered=True``) never reach the user, full stop
-- no blacklist to keep extending. They remain in the persisted
VerifyProductResult/cache for diagnostics; only what's *displayed* is
narrowed here.

The one remaining edge case an allowlist can't catch: a raw label
coincidentally matching a canonical alias (e.g. a help-center page's
"Wi-Fi" toggle-instructions row satisfying the smartphone schema's "wifi"
alias) can still smuggle unusable prose into an otherwise legitimate
canonical field. ``has_usable_value`` is a narrow value-shape safety net for
exactly that: a canonical field with a foreign-script or RF-emission-limit
value renders as not-found rather than leaking that text, without dropping
the field/column itself (a missing canonical attribute still needs a stable,
present, empty column -- see bot.export.build_wide_export_row).

Only bot.formatters and bot.export import this module: it never touches
services.product_verifier data, only decides which already-computed
ServiceAttribute rows/values are shown. Rerunning a cached result with a
newer filter changes what's displayed without re-verifying anything.
"""

from __future__ import annotations

import re
from typing import Iterable

from services.product_verifier import ServiceAttribute


_RF_UNIT_RE = re.compile(r"dbm|dbua|dbµa", re.IGNORECASE)
# Non-Latin, non-Cyrillic scripts (CJK/Hangul/Kana and similar): our
# canonical schema and UI never use them, so a canonical field holding one
# is either mojibake or page furniture the pipeline mapped by accident.
_FOREIGN_SCRIPT_RE = re.compile(
    r"[぀-ヿ㐀-䶿一-鿿가-힣豈-﫿]"
)


# core.schema's universal identity fields already appear in the Telegram
# summary line / bot.export's identity columns (sourced from
# VerifyProductResult.identity, the resolved single value) -- they also
# exist as their own ServiceAttribute in result.attributes (one raw fact
# among possibly several), so without this exclusion they'd render a second
# time in the attribute list/CSV columns, redundant with -- and sometimes
# inconsistent with -- the identity-derived one.
IDENTITY_CANONICAL_NAMES = frozenset({"brand", "model", "manufacturer_article"})


def is_user_facing(attribute: ServiceAttribute) -> bool:
    """Strict allowlist: only canonical category-schema fields, once each.

    ``discovered`` is core.profile's own flag for "not in the schema" (see
    core.schema.extend_schema_with_discovered) -- an extraction leftover,
    however plausible-looking, is never shown automatically. Surfacing a
    specific discovered field is a deliberate future product decision (a
    named schema addition), not an automatic passthrough.
    """
    if attribute.canonical_name in IDENTITY_CANONICAL_NAMES:
        return False
    return not attribute.discovered


def has_usable_value(attribute: ServiceAttribute) -> bool:
    """Whether a canonical attribute's value is safe to show as-is.

    Guards only against the rare alias collision (see module docstring) --
    it never excludes the attribute/column, only blanks an unusable value so
    the field renders the same as "not found".
    """
    value = attribute.value
    if value is None or isinstance(value, dict):
        return True
    text = str(value)
    return not (_FOREIGN_SCRIPT_RE.search(text) or _RF_UNIT_RE.search(text))


def effective_status(attribute: ServiceAttribute) -> str:
    """Confirmed-but-unusable renders (and counts, for bucketing) as Unresolved."""
    if attribute.status == "Confirmed" and not has_usable_value(attribute):
        return "Unresolved"
    return attribute.status


def filter_user_facing(attributes: Iterable[ServiceAttribute]) -> list[ServiceAttribute]:
    """Every attribute worth showing a shopper, in its original order."""
    return [item for item in attributes if is_user_facing(item)]
