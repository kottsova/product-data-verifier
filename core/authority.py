"""Evidence-based manufacturer/official-domain authority recovery.

``core.discovery.discover_global_official_domains`` only trusts an explicit
"official site" search-engine snippet naming the brand. Many real
manufacturer or brand-operated domains never produce such a snippet, so
authority for them stays "unknown" even though the fetched page itself
proves the relationship. This module recovers that authority from the
fetched page's own content: structured Organization/Brand/Product data, a
site-identity meta tag, or a copyright line that names the brand as the
site's owner.

Domain/brand name similarity is a necessary co-signal, never sufficient by
itself — a multi-brand retailer whose domain happens to start with the
brand name, or an unrelated company that merely mentions the brand in body
text, must not be classified as authoritative.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import re

from bs4 import BeautifulSoup

from core.match import normalize_model, normalize_text


@dataclass(frozen=True, slots=True)
class AuthorityEvidence:
    verified: bool
    signal: str | None = None
    reason: str | None = None


_CORPORATE_SUFFIXES = frozenset({
    "inc", "incorporated", "corp", "corporation", "co", "company",
    "ltd", "limited", "llc", "gmbh", "ag", "sa", "spa", "srl", "kk",
    "group", "holding", "holdings", "plc",
    "ооо", "оао", "зао", "пао", "ип",
})


def _brand_key(value: str) -> str:
    return "".join(normalize_text(value).split())


def _entity_key(value: str) -> str:
    tokens = normalize_text(value).split()
    while tokens and tokens[-1] in _CORPORATE_SUFFIXES:
        tokens.pop()
    return "".join(tokens)


def _entity_matches_brand(entity: str, brand_key: str) -> bool:
    return bool(brand_key) and _entity_key(entity) == brand_key


def _domain_label(domain: str) -> str:
    return (domain or "").split(".")[0]


def _brand_domain_consistent(domain: str, brand: str) -> bool:
    brand_key = normalize_model(brand)
    label_key = normalize_model(_domain_label(domain))
    return (
        len(brand_key) >= 3
        and bool(label_key)
        and (label_key == brand_key or label_key.startswith(brand_key) or brand_key.startswith(label_key))
    )


def _structured_data_signal(soup: BeautifulSoup, brand_key: str) -> str | None:
    for script in soup.find_all("script", attrs={"type": re.compile(r"application/ld\+json", re.I)}):
        raw = script.string or script.get_text() or ""
        if len(raw) > 1_000_000:
            continue
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        pending = [payload]
        while pending:
            node = pending.pop()
            if isinstance(node, list):
                pending.extend(node)
                continue
            if not isinstance(node, dict):
                continue
            node_type = node.get("@type", [])
            node_types = node_type if isinstance(node_type, list) else [node_type]
            types_casefold = {str(item).casefold() for item in node_types}
            if types_casefold & {"organization", "brand", "corporation"}:
                name = node.get("name")
                if isinstance(name, str) and _entity_matches_brand(name, brand_key):
                    return "structured_data_organization"
            if "product" in types_casefold:
                for field in ("brand", "manufacturer"):
                    value = node.get(field)
                    candidate_name = value.get("name") if isinstance(value, dict) else value
                    if isinstance(candidate_name, str) and _entity_matches_brand(candidate_name, brand_key):
                        return "structured_data_product_brand"
            pending.extend(node.values())
    return None


def _site_name_meta_signal(soup: BeautifulSoup, brand_key: str) -> str | None:
    for attr in ("property", "name"):
        for prop in ("og:site_name", "application-name"):
            node = soup.find("meta", attrs={attr: re.compile(f"^{re.escape(prop)}$", re.I)})
            content = str(node.get("content") or "") if node else ""
            if content and _entity_matches_brand(content, brand_key):
                return "site_name_meta"
    return None


_COPYRIGHT_PATTERN = re.compile(
    r"(?:©|\(c\)|copyright)\s*\d{4}(?:\s*[-–—]\s*\d{4})?\s+([^.,;\n]{1,60})",
    re.IGNORECASE,
)


def _copyright_signal(text: str, brand_key: str) -> str | None:
    for match in _COPYRIGHT_PATTERN.finditer(text or ""):
        if _entity_matches_brand(match.group(1), brand_key):
            return "copyright_entity"
    return None


def evaluate_content_authority(html: str, text: str, domain: str, brand: str) -> AuthorityEvidence:
    """Return generic, evidence-based manufacturer authority for one fetched page.

    Requires brand/domain-name consistency AND at least one independent
    content signal (structured Organization/Brand/Product data, a
    site-identity meta tag, or a copyright line) that itself names the
    brand. Neither signal alone is sufficient.
    """
    if not brand or not domain:
        return AuthorityEvidence(False)
    if not _brand_domain_consistent(domain, brand):
        return AuthorityEvidence(False)
    brand_key = _brand_key(brand)
    if not brand_key:
        return AuthorityEvidence(False)
    soup = BeautifulSoup(html or "", "html.parser")
    signal = (
        _structured_data_signal(soup, brand_key)
        or _site_name_meta_signal(soup, brand_key)
        or _copyright_signal(text, brand_key)
    )
    if signal is None:
        return AuthorityEvidence(False)
    return AuthorityEvidence(
        True, signal=signal,
        reason=f"brand/domain consistency plus {signal} evidence naming the brand as site owner",
    )
