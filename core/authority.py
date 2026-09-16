"""Evidence-based source-role authority recovery.

``core.discovery.discover_global_official_domains`` trusts either an explicit
"official site" search-engine snippet naming the brand or the stricter
combination of exact brand/root-domain equality, official-intent search
provenance, and a separate exact-model result on that domain. Many real
manufacturer-, distributor-, or dealer-operated domains never produce such a
snippet, so authority for them stays "unknown" even though the fetched page
itself carries evidence of the relationship. This module recovers a
generic, safe *role* for such a page instead of a binary verified/unknown
flag, because a site that credibly claims a relationship with the brand is
not automatically its manufacturer:

    manufacturer | official_distributor | authorized_dealer
    | retailer | marketplace | unknown

Two evidence tiers, deliberately kept separate:

1. Self-declared signals (copyright, structured Organization/Brand/Product
   data, a site-identity meta tag, "official distributor"/"authorized
   dealer" wording, brand/domain-name consistency). These are necessary but
   never sufficient on their own to grant an elevated role - anyone can put
   "official distributor" in their own page text.
2. Independent corroboration: an already-trusted source in the same run
   (typically the brand's own SERP-verified manufacturer/official domain)
   linking to the candidate's domain. Only corroboration elevates a
   self-declared claim to the matching role. Without it, the claim
   downgrades to "retailer" (we know it is commercial and brand-related,
   just not that the claimed relationship is proven) or "unknown" (no
   brand-identifying evidence at all).

Brand/domain consistency, wording, and content signals are generic checks
against the requested product's own brand - nothing here hardcodes a brand
or domain name.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Iterable, Literal
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from core.discovery import _is_marketplace_domain
from core.match import normalize_model, normalize_text


Role = Literal[
    "manufacturer", "official_distributor", "authorized_dealer",
    "retailer", "marketplace", "unknown",
]

# Roles that require independent corroboration before they may be granted.
ELEVATED_ROLES: tuple[Role, ...] = ("manufacturer", "official_distributor", "authorized_dealer")


@dataclass(frozen=True, slots=True)
class SelfDeclaredClaim:
    """The role a page's own content claims, before any corroboration."""

    role: Role
    signal: str | None = None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class CorroborationResult:
    """Independent, cross-domain confirmation of a self-declared claim."""

    found: bool
    evidence_domain: str | None = None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class AuthorityAssessment:
    role: Role
    self_declared: SelfDeclaredClaim
    corroboration: CorroborationResult
    reason: str


@dataclass(frozen=True, slots=True)
class TrustedSource:
    """An already-trusted page in the same run, used to corroborate others."""

    domain: str
    html: str


_CORPORATE_SUFFIXES = frozenset({
    "inc", "incorporated", "corp", "corporation", "co", "company",
    "ltd", "limited", "llc", "gmbh", "ag", "sa", "spa", "srl", "kk",
    "group", "holding", "holdings", "plc",
    "ооо", "оао", "зао", "пао", "ип",
})

# Self-declared relationship wording. Necessary evidence for an elevated
# claim, never sufficient by itself (see module docstring).
_DISTRIBUTOR_MARKERS = (
    "official distributor", "authorized distributor", "exclusive distributor",
    "official importer", "authorized importer", "official supplier",
    "official supply", "official deliveries",
    "официальный дистрибьютор", "эксклюзивный дистрибьютор",
    "официальный импортер", "официальный импортёр",
    "официальные поставки", "официальный поставщик",
)
_DEALER_MARKERS = (
    "authorized dealer", "official dealer", "authorized reseller",
    "official reseller", "certified dealer", "official partner",
    "authorized partner", "official retailer", "official seller",
    "официальный дилер", "авторизованный дилер", "официальный партнер",
    "официальный партнёр", "официальный продавец", "официальный представитель",
    "авторизованный реселлер", "официальный реселлер",
)


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


def _relationship_wording(haystack: str) -> tuple[Role, str] | None:
    lowered = haystack.casefold()
    if any(marker in lowered for marker in _DISTRIBUTOR_MARKERS):
        return "official_distributor", "distributor_wording"
    if any(marker in lowered for marker in _DEALER_MARKERS):
        return "authorized_dealer", "dealer_wording"
    return None


def classify_self_declared_role(html: str, text: str, domain: str, brand: str) -> SelfDeclaredClaim:
    """Return the role a page's own content claims, before corroboration.

    Only ever returns an elevated role (manufacturer/official_distributor/
    authorized_dealer) or "unknown" - never "retailer" or "marketplace",
    which are decided by ``resolve_authority`` using information this
    function does not have (marketplace-domain lists, corroboration).
    """
    if not brand or not domain:
        return SelfDeclaredClaim("unknown")
    if not _brand_domain_consistent(domain, brand):
        return SelfDeclaredClaim("unknown")
    brand_key = _brand_key(brand)
    if not brand_key:
        return SelfDeclaredClaim("unknown")

    wording = _relationship_wording(f"{text} {html}")
    if wording:
        role, signal = wording
        return SelfDeclaredClaim(
            role, signal,
            f"Brand/domain consistency plus self-declared {signal.replace('_', ' ')}.",
        )

    soup = BeautifulSoup(html or "", "html.parser")
    entity_signal = (
        _structured_data_signal(soup, brand_key)
        or _site_name_meta_signal(soup, brand_key)
        or _copyright_signal(text, brand_key)
    )
    if entity_signal:
        return SelfDeclaredClaim(
            "manufacturer", entity_signal,
            f"Brand/domain consistency plus {entity_signal} names only the brand as site owner.",
        )
    return SelfDeclaredClaim("unknown")


def _link_hostnames(html: str) -> set[str]:
    soup = BeautifulSoup(html or "", "html.parser")
    hosts: set[str] = set()
    for anchor in soup.find_all("a", href=True):
        href = str(anchor.get("href") or "")
        try:
            host = (urlparse(href).hostname or "").lower().removeprefix("www.")
        except ValueError:
            continue
        if host:
            hosts.add(host)
    return hosts


def find_corroboration(domain: str, trusted_sources: Iterable[TrustedSource]) -> CorroborationResult:
    """Return independent, cross-domain confirmation of a candidate domain.

    A trusted source (typically the brand's own already-verified domain)
    linking to the candidate's domain is generic, brand-agnostic evidence of
    a real relationship - unlike anything the candidate says about itself.
    """
    target = (domain or "").lower().removeprefix("www.")
    if not target:
        return CorroborationResult(False)
    for anchor in trusted_sources:
        anchor_domain = (anchor.domain or "").lower().removeprefix("www.")
        for host in _link_hostnames(anchor.html):
            if host == target or host.endswith(f".{target}") or target.endswith(f".{host}"):
                return CorroborationResult(
                    True, anchor_domain,
                    f"{anchor_domain} links to {target}.",
                )
    return CorroborationResult(False)


def resolve_authority(
    html: str, text: str, domain: str, brand: str,
    trusted_sources: Iterable[TrustedSource] = (),
) -> AuthorityAssessment:
    """Resolve a fetched page's authority role from content evidence alone.

    Safety invariants:
    - ``brand-domain match + copyright`` never yields "manufacturer" by
      itself - it only yields a self-declared *claim*.
    - ``site says "official"`` never yields an elevated role by itself.
    - An elevated role requires independent corroboration; without it, the
      claim downgrades to "retailer" (evidence of a commercial site, no
      proven relationship) or "unknown" (no brand-identifying evidence).
    """
    if _is_marketplace_domain(domain):
        return AuthorityAssessment(
            "marketplace", SelfDeclaredClaim("marketplace"), CorroborationResult(False),
            "Marketplace domain; platform-level listings are never brand authority.",
        )

    claim = classify_self_declared_role(html, text, domain, brand)
    if claim.role == "unknown":
        return AuthorityAssessment(
            "unknown", claim, CorroborationResult(False),
            "No brand-identifying self-declared evidence found.",
        )

    corroboration = find_corroboration(domain, trusted_sources)
    if corroboration.found:
        return AuthorityAssessment(
            claim.role, claim, corroboration,
            f"Self-declared {claim.role} corroborated: {corroboration.reason}",
        )
    return AuthorityAssessment(
        "retailer", claim, corroboration,
        f"Self-declared {claim.role} claim ({claim.signal}) has no independent corroboration; "
        "treated as an unconfirmed retailer, not elevated.",
    )
