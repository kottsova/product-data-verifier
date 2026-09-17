"""Generic first-stage discovery of plausible product source pages."""

from __future__ import annotations

import base64
from dataclasses import dataclass, field, replace
from html import unescape
from html.parser import HTMLParser
import os
from pathlib import Path
import re
import tempfile
import time
from typing import Callable, Iterable, Literal, Mapping, Protocol, TypedDict
from urllib.parse import parse_qsl, parse_qs, quote_plus, unquote, urlencode, urlparse, urlunparse
from urllib.request import Request, urlopen

import requests

from core.budget import WallClockBudget
from core.match import article_matches, candidate_model_match, model_match, normalize_model, normalize_text
from core.identity import ProductIdentity, base_model_in_text, identity_verification_signals
from core.provider_health import (
    FailureClass,
    HEALTH_AFFECTING_FAILURE_CLASSES,
    ProviderHealthStore,
)

RETRY_BACKOFF_SECONDS = 0.3


class Candidate(TypedDict):
    """Ranked source; model_match is legacy, model_relevance is explicit."""

    url: str
    domain: str
    title: str
    source_type: str
    authority_status: str
    authority_evidence_url: str | None
    authority_reason: str | None
    product_match_evidence: str | None
    market_scope: str
    model_match: str
    model_relevance: str
    score: int
    identity_relation: str
    identity_verification_evidence: list[str]
    relevance_relation: str
    relevance_reasons: list[str]
    snippet: str
    discovery_provider: str
    discovery_query: str
    discovery_rank: int
    raw_url: str
    redirect_url: str | None
    parse_status: str
    parse_confidence: str
    discovery_provenance: list[dict[str, object]]


SearchResult = tuple[str, str]
@dataclass(frozen=True, slots=True)
class SearchResultRecord:
    """Provider-neutral result link before product relevance/ranking."""

    url: str
    title: str
    snippet: str = ""
    provider: str = "unknown"
    query: str = ""
    rank: int = 0
    raw_url: str = ""
    redirect_url: str | None = None
    parse_status: str = "parsed"
    parse_confidence: str = "medium"


SearchResultLike = SearchResult | SearchResultRecord
SearchStatus = Literal["success", "partial", "empty", "blocked", "timeout", "error"]
ProviderStatus = Literal[
    "success", "empty", "blocked", "timeout", "parse_error", "error", "circuit_open",
    "capped", "low_value",
]
RelevanceRelation = Literal["exact", "likely_variant", "weak", "reject"]


class SearchProvider(Protocol):
    """Interchangeable transport for one web-search provider."""

    name: str

    def search(self, query: str) -> Iterable[SearchResultLike]: ...


@dataclass(frozen=True, slots=True)
class ProviderAttempt:
    """Search-provider reliability metadata, independent of source authority."""

    provider: str
    query: str
    status: ProviderStatus
    result_count: int = 0
    message: str | None = None
    is_fallback: bool = False
    duration_seconds: float = 0.0
    timeout_seconds: float | None = None
    timed_out: bool = False
    blocked: bool = False
    parse_failure: bool = False
    exception_class: str | None = None
    circuit_open: bool = False
    budget_exhausted: bool = False
    provider_time_capped: bool = False
    raw_result_count: int = 0
    parsed_result_count: int = 0
    deduped_result_count: int = 0
    transport: str | None = None
    failure_class: str | None = None
    shared_circuit_open: bool = False
    retried: bool = False
    effective_query: str | None = None
    exact_model_hit: bool = False
    official_domain_hit: bool = False
    budget_before_seconds: float | None = None
    budget_after_seconds: float | None = None
    independent_success_without_ddg: bool = False
    accepted_candidate_count: int = 0
    rejected_candidate_count: int = 0


@dataclass(frozen=True, slots=True)
class ProviderQueryOutcome:
    results: tuple[SearchResultLike, ...] = ()
    attempts: tuple[ProviderAttempt, ...] = ()


Searcher = Callable[[str], Iterable[SearchResultLike] | ProviderQueryOutcome]


@dataclass(frozen=True, slots=True)
class DiscoveryIssue:
    status: Literal[
        "empty", "blocked", "timeout", "parse_error", "error", "circuit_open", "capped",
        "low_value",
    ]
    query: str
    message: str
    provider: str | None = None


@dataclass(frozen=True, slots=True)
class DiscoveryOutcome:
    """Structured discovery result; degraded search is never an empty success."""

    candidates: list[Candidate] = field(default_factory=list)
    search_status: SearchStatus = "success"
    queries: list[str] = field(default_factory=list)
    attempted_queries: list[str] = field(default_factory=list)
    issues: list[DiscoveryIssue] = field(default_factory=list)
    provider_attempts: list[ProviderAttempt] = field(default_factory=list)
    rejected_candidates: list[Candidate] = field(default_factory=list)


class DiscoverySearchError(RuntimeError):
    """Compatibility exception carrying the structured failed outcome."""

    def __init__(self, outcome: DiscoveryOutcome) -> None:
        self.outcome = outcome
        detail = outcome.issues[-1].message if outcome.issues else "Search failed."
        super().__init__(f"Discovery {outcome.search_status}: {detail}")


class ProviderTimeoutError(RuntimeError):
    """A provider exhausted its own configured deadline."""


class ProviderParseError(RuntimeError):
    """A provider returned a result page that could not be parsed safely."""


DEFAULT_PROVIDER_TIMEOUTS: dict[str, float] = {
    "bing": 8.0,
    "google": 8.0,
    "duckduckgo_html": 8.0,
    "duckduckgo_lite": 8.0,
    "naver": 8.0,
    "seznam": 8.0,
    "direct_domain_probe": 6.0,
}


@dataclass(frozen=True, slots=True)
class DiscoveryRuntimeConfig:
    """Per-provider execution limits and request-local circuit policy."""

    provider_timeouts: Mapping[str, float] = field(
        default_factory=lambda: dict(DEFAULT_PROVIDER_TIMEOUTS),
    )
    circuit_breaker_failures: int = 1
    provider_time_share: float = 0.25

    def __post_init__(self) -> None:
        normalized = dict(DEFAULT_PROVIDER_TIMEOUTS)
        normalized.update({str(name): float(value) for name, value in self.provider_timeouts.items()})
        if any(value <= 0 for value in normalized.values()):
            raise ValueError("provider timeouts must be positive")
        if self.circuit_breaker_failures < 1:
            raise ValueError("circuit_breaker_failures must be positive")
        if not (0.0 < self.provider_time_share <= 1.0):
            raise ValueError("provider_time_share must be within (0, 1]")
        object.__setattr__(self, "provider_timeouts", normalized)

    def timeout_for(self, provider: str) -> float:
        return float(self.provider_timeouts.get(provider, 10.0))

MARKETPLACE_DOMAINS = {
    "amazon", "aliexpress", "ebay", "ozon", "temu", "wildberries",
}
BLOCKED_DOMAINS = {
    "facebook.com", "instagram.com", "linkedin.com",
    "pinterest.com", "tiktok.com", "twitter.com", "vk.com", "x.com",
    "youtube.com", "youtu.be",
}
BLOCKED_EXACT_HOSTS = {"google.com"}
BLOCKED_PATH_SEGMENTS = {
    "about", "account", "blog", "contact", "login", "news", "newsroom", "privacy",
    "register", "signin", "terms", "warranty",
}
CATALOG_SEGMENTS = {
    "catalog", "category", "categories", "collection", "collections",
    "products", "search", "shop", "tag", "tags",
}
PRODUCT_HINTS = {"item", "p", "product", "products", "sku"}
SUPPORT_PATH_HINTS = {
    "document", "documents", "download", "downloads", "manual", "manuals",
    "productservice", "service", "spec", "specification", "specifications",
    "support", "supportdetail",
}
WEAK_PATH_HINTS = {
    "compare", "comparison", "forum", "forums", "offersofproduct", "questions",
    "review", "reviews", "test", "testbericht", "tests", "threads",
    "preisvergleich", "toplist",
}
ACCESSORY_CONTEXT_TERMS = {
    "assembly", "case", "cover", "digitizer", "protector", "replacement",
    "refurbished", "renewed", "spare", "wallet", "hülle", "кейс", "чехол", "케이스",
}
SPECIALIZED_REFERENCE_DOMAINS = {
    "gsmarena.com", "manua.ls", "manuals.co.uk", "manualslib.com",
    "manuals.plus", "manymanuals.com", "nanoreview.net",
}
NON_PRODUCT_CONTEXT_TERMS = {
    "athlete", "biography", "coach", "defender", "football", "forward",
    "goalkeeper", "highlights", "interview", "midfielder", "player",
    "roster", "soccer", "sports", "transfer",
}
NON_PRODUCT_CONTEXT_DOMAINS = {
    "espn.com", "fifa.com", "imdb.com", "transfermarkt.com", "wikipedia.org",
}
NON_PRODUCT_CONTEXT_PATHS = {
    "athlete", "biography", "forum", "forums", "people", "person", "profile",
    "roster", "sports", "wiki",
}
TRACKING_PARAMETERS = {"fbclid", "gclid", "srsltid", "yclid"}

SCORE_EXACT_MODEL = 60
SCORE_LIKELY_MODEL = 30
SCORE_LIKELY_VARIANT = 45
SCORE_UNKNOWN_MODEL = -35
SCORE_MISMATCH = -120
SCORE_ARTICLE = 35
SCORE_MANUFACTURER = 25
SCORE_OFFICIAL_DOCUMENT = 20
SCORE_VERIFIED_AUTHORITY = 20
SCORE_RETAILER = 5
SCORE_PRODUCT_PAGE = 15
SCORE_MODEL_IN_URL = 10
SCORE_MODEL_IN_TITLE = 10
SCORE_MARKETPLACE = -25
SCORE_HOMEPAGE = -25
SCORE_CATALOG = -20
SCORE_SUPPORT_PAGE = 10
SCORE_WEAK_PAGE = -20
SCORE_BRAND_DOMAIN_EXACT = 30

SUPPORTED_MARKETS = {"global", "US", "GB", "DE", "RU"}
MARKET_GOOGLE_PARAMS = {
    "global": {"hl": "en", "pws": "0"},
    "US": {"hl": "en", "gl": "us", "pws": "0"},
    "GB": {"hl": "en", "gl": "gb", "pws": "0"},
    "DE": {"hl": "de", "gl": "de", "pws": "0"},
    "RU": {"hl": "ru", "gl": "ru", "pws": "0"},
}

# Stage 2 will build a complete product profile. GTIN/EAN/UPC is an optional,
# strong identity signal: its absence must not downgrade an otherwise valid
# brand/model/article match. It is especially useful for regional variants.
# Market scope may be global, regional, or unknown and must not be assumed.
# The profile must also keep product dimensions
# separate from package dimensions, net weight separate from gross/shipping
# weight, and prioritize identifiers such as GTIN/EAN/UPC and MPN/article.

_OFFICIAL_DOMAIN_CACHE: dict[tuple[str, str], tuple[tuple[str, str], ...]] = {}


def browser_headless() -> bool:
    """Use headless Chromium unless explicitly disabled for local debugging."""
    return os.getenv("PDV_BROWSER_HEADLESS", "true").strip().lower() not in {"0", "false", "no", "off"}


def clear_official_domain_cache() -> None:
    """Clear the process-local cache, primarily for deterministic tests."""
    _OFFICIAL_DOMAIN_CACHE.clear()


def build_search_queries(brand: str, model: str, article: str | None = None) -> list[str]:
    brand = " ".join((brand or "").split())
    model = " ".join((model or "").split())
    queries = [
        f"{brand} {model}",
        f'"{brand} {model}"',
        f'"{model}" {brand}',
        f'"{model}" {brand} specifications',
    ]
    if article and article.strip():
        article = " ".join(article.split())
        queries.extend((f'{brand} "{article}"', f'"{model}" "{article}" {brand}'))
    return list(dict.fromkeys(query.strip() for query in queries if query.strip()))


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower().removeprefix("www.")


_COUNTRY_CODE_SECOND_LEVEL_DOMAINS = {
    "ac.uk", "co.jp", "co.kr", "co.nz", "co.uk", "com.au", "com.br",
    "com.cn", "com.hk", "com.mx", "com.sg", "com.tr", "com.tw",
}


def _registrable_domain(domain: str) -> str:
    """Return the root domain needed for a conservative brand equality check."""
    labels = (domain or "").lower().removeprefix("www.").split(".")
    if len(labels) < 2:
        return labels[0] if labels else ""
    suffix = ".".join(labels[-2:])
    if suffix in _COUNTRY_CODE_SECOND_LEVEL_DOMAINS and len(labels) >= 3:
        return ".".join(labels[-3:])
    return suffix


def _registrable_domain_label(domain: str) -> str:
    root = _registrable_domain(domain)
    return re.sub(r"[^a-z0-9]", "", root.split(".")[0])


def url_belongs_to_domain(url: str, domain: str) -> bool:
    host = _host(url)
    domain = (domain or "").lower().removeprefix("www.")
    return bool(host and domain) and (host == domain or host.endswith(f".{domain}"))


def canonicalize_url(url: str) -> str:
    parsed = urlparse((url or "").strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return ""
    host = parsed.hostname.lower().removeprefix("www.")
    if parsed.port and parsed.port not in {80, 443}:
        host = f"{host}:{parsed.port}"
    path = re.sub(r"/{2,}", "/", parsed.path or "/")
    if path != "/":
        path = path.rstrip("/")
    query = urlencode([
        (key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in TRACKING_PARAMETERS
    ])
    return urlunparse((parsed.scheme.lower(), host, path, "", query, ""))


def _search_result_record(
    item: SearchResultLike,
    *,
    provider: str = "unknown",
    query: str = "",
    rank: int = 0,
) -> SearchResultRecord:
    if isinstance(item, SearchResultRecord):
        return SearchResultRecord(
            url=item.url,
            title=item.title,
            snippet=item.snippet,
            provider=item.provider if item.provider != "unknown" else provider,
            query=item.query or query,
            rank=item.rank or rank,
            raw_url=item.raw_url or item.url,
            redirect_url=item.redirect_url,
            parse_status=item.parse_status,
            parse_confidence=item.parse_confidence,
        )
    raw_url, title = item
    return SearchResultRecord(
        url=raw_url,
        title=title,
        provider=provider,
        query=query,
        rank=rank,
        raw_url=raw_url,
    )


def _normalized_search_results(
    results: Iterable[SearchResultLike],
    provider: str,
    query: str,
) -> tuple[SearchResultRecord, ...]:
    return tuple(
        _search_result_record(item, provider=provider, query=query, rank=index)
        for index, item in enumerate(results, start=1)
    )


def is_obvious_non_product_url(url: str) -> bool:
    if not url or not _host(url):
        return True
    host = _host(url)
    if host in BLOCKED_EXACT_HOSTS:
        return True
    if any(host == blocked or host.endswith(f".{blocked}") for blocked in BLOCKED_DOMAINS):
        return True
    segments = {segment.lower() for segment in urlparse(url).path.split("/") if segment}
    return bool(segments & BLOCKED_PATH_SEGMENTS)


def _path_has_hint(segments: Iterable[str], hints: set[str]) -> bool:
    return any(
        token in hints
        for segment in segments
        for token in re.findall(r"[a-z0-9]+", segment.lower())
    )


def _page_kind(url: str) -> str:
    parsed = urlparse(url)
    segments = [segment.lower() for segment in parsed.path.split("/") if segment]
    if not segments or (len(segments) == 1 and len(segments[0]) <= 3):
        return "homepage"
    if _path_has_hint(segments, SUPPORT_PATH_HINTS) or parsed.path.lower().endswith(".pdf"):
        return "support"
    if segments[-1] in CATALOG_SEGMENTS or "search" in parse_qs(parsed.query):
        return "catalog"
    if _path_has_hint(segments, WEAK_PATH_HINTS):
        return "weak"
    if set(segments) & PRODUCT_HINTS:
        return "product"
    return "other"


def discover_global_official_domains(
    brand: str,
    results: Iterable[SearchResultLike],
    *,
    product_results: Iterable[SearchResultLike] = (),
    model: str | None = None,
) -> list[tuple[str, str]]:
    """Return conservatively proven official domains and their evidence URLs.

    An explicit search-result claim remains sufficient. A provider that omits
    the word "official" may use the stricter fallback: exact brand/root-domain
    equality plus a separate exact-model result on that same root domain.
    """
    brand_key = normalize_model(brand).lower()
    product_records = [
        _search_result_record(item, rank=position + 1)
        for position, item in enumerate(product_results)
    ]
    ranked: list[tuple[int, str, str]] = []
    for position, item in enumerate(results):
        record = _search_result_record(item, rank=position + 1)
        url, title = record.url, f"{record.title} {record.snippet}".strip()
        domain = _host(url)
        label = re.sub(r"[^a-z0-9]", "", domain.split(".")[0])
        root_label = _registrable_domain_label(domain)
        title_norm = normalize_text(title)
        label_consistent = brand_key in label or label in brand_key
        exact_root = root_label == re.sub(r"[^a-z0-9]", "", brand_key)
        if not brand_key or not (label_consistent or exact_root):
            continue
        brand_in_title = normalize_model(brand) in normalize_model(title)
        official_signal = "official" in title_norm or "официаль" in title_norm
        # Domain/brand similarity is only a consistency check. Verification
        # requires either an explicit claim or corroborating exact-product
        # search evidence; a similarly named homepage is not enough.
        if official_signal and brand_in_title:
            ranked.append((120 - position, domain, canonicalize_url(url)))
            continue

        root_domain = _registrable_domain(domain)
        evidence_url = canonicalize_url(url)
        exact_brand_root = (
            len(brand_key) >= 3
            and exact_root
        )
        exact_product_result = next((
            product
            for product in product_records
            if model
            and canonicalize_url(product.url) != evidence_url
            and url_belongs_to_domain(product.url, root_domain)
            and model_match(
                model,
                f"{product.title} {product.snippet} {product.url}",
            ) == "exact"
        ), None)
        if exact_brand_root and brand_in_title and exact_product_result is not None:
            ranked.append((100 - position, root_domain, evidence_url))
    # Provider result sets sometimes omit the homepage/"official" result but
    # do return an exact product page on a mechanically exact brand root,
    # including regional public suffixes (brand.de, brand.co.uk, and so on).
    # Exact registrable label equality + brand in title + exact model in
    # title/URL is sufficient corroboration; snippets, fuzzy prefixes, and
    # parent-company domains are not.
    for position, product in enumerate(product_records):
        domain = _host(product.url)
        root_domain = _registrable_domain(domain)
        expected_label = re.sub(r"[^a-z0-9]", "", brand_key)
        suffix = root_domain.rsplit(".", 1)[-1]
        if (
            suffix in {"example", "invalid", "localhost", "test"}
            or len(expected_label) < 2
            or _registrable_domain_label(domain) != expected_label
        ):
            continue
        title_url = f"{product.title} {product.url}"
        if (
            normalize_model(brand) in normalize_model(product.title)
            and model
            and candidate_model_match(model, product.title, urlparse(product.url).path) == "exact"
        ):
            ranked.append((90 - position, root_domain, canonicalize_url(product.url)))
    ranked.sort(reverse=True)
    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    for _, domain, evidence_url in ranked:
        if domain not in seen:
            seen.add(domain)
            found.append((domain, evidence_url))
    return found


def discover_global_official_domain(brand: str, results: Iterable[SearchResultLike]) -> str | None:
    """Backward-compatible single-domain view of official-domain discovery."""
    domains = discover_global_official_domains(brand, results)
    return domains[0][0] if domains else None


def _is_marketplace_domain(domain: str) -> bool:
    labels = set(domain.lower().removeprefix("www.").split("."))
    return bool(labels & MARKETPLACE_DOMAINS)


def _is_official_document(url: str) -> bool:
    parsed = urlparse(url)
    path = parsed.path.lower()
    segments = [segment for segment in path.split("/") if segment]
    host_label = (parsed.hostname or "").lower().removeprefix("www.").split(".")[0]
    return (
        path.endswith(".pdf")
        or host_label in {"docs", "manuals", "support"}
        or _path_has_hint(segments, SUPPORT_PATH_HINTS)
    )


def _brand_domain_match(brand: str | None, domain: str) -> bool:
    """Return a ranking-only brand/domain affinity signal.

    This never verifies authority or changes source_type. Requiring an exact
    model match separately prevents an unrelated same-name corporate homepage
    from receiving a product-source boost.
    """
    brand_key = normalize_model(brand).casefold()
    domain_label = normalize_model(domain.split(".")[0]).casefold()
    return len(brand_key) >= 4 and domain_label.startswith(brand_key)


def classify_source(domain: str, official_domain: str | None) -> str:
    # Marketplace exclusion is checked first and unconditionally: a brand
    # whose name coincides with a marketplace domain label (e.g. a brand
    # literally named after a marketplace) must never let an "official site"
    # SERP match promote that marketplace domain itself to manufacturer.
    if _is_marketplace_domain(domain):
        return "marketplace"
    root = _registrable_domain(domain)
    if root in SPECIALIZED_REFERENCE_DOMAINS:
        return "specialized_reference"
    if official_domain and (domain == official_domain or domain.endswith(f".{official_domain}")):
        return "manufacturer"
    if any(word in domain for word in (
        "bestbuy", "citilink", "currys", "digitec", "dns-shop", "galaxus",
        "kaufland", "mediamarkt", "mvideo", "otto", "shop", "store",
        "retail", "technopark",
    )):
        return "retailer"
    return "other"


def score_candidate(url: str, title: str, source_type: str, authority_status: str,
                    match: str, model: str, article: str | None,
                    brand: str | None = None) -> int:
    score = {"exact": SCORE_EXACT_MODEL, "likely_variant": SCORE_LIKELY_VARIANT,
             "likely": SCORE_LIKELY_MODEL, "mismatch": SCORE_MISMATCH,
             "different_variant": SCORE_MISMATCH,
             "unknown": SCORE_UNKNOWN_MODEL}[match]
    haystack = f"{title} {url}"
    if article_matches(article, haystack):
        score += SCORE_ARTICLE
    if source_type == "manufacturer":
        score += SCORE_MANUFACTURER
    elif source_type == "official_document":
        score += SCORE_OFFICIAL_DOCUMENT
    elif source_type == "retailer":
        score += SCORE_RETAILER
    elif source_type == "marketplace":
        score += SCORE_MARKETPLACE
    if authority_status == "verified":
        score += SCORE_VERIFIED_AUTHORITY
    kind = _page_kind(url)
    score += {"product": SCORE_PRODUCT_PAGE, "homepage": SCORE_HOMEPAGE,
              "catalog": SCORE_CATALOG, "support": SCORE_SUPPORT_PAGE,
              "weak": SCORE_WEAK_PAGE, "other": 0}[kind]
    expected = normalize_model(model)
    if expected and expected in normalize_model(title):
        score += SCORE_MODEL_IN_TITLE
    if expected and expected in normalize_model(urlparse(url).path):
        score += SCORE_MODEL_IN_URL
    if match in {"exact", "likely_variant"} and _brand_domain_match(brand, _host(url)):
        score += SCORE_BRAND_DOMAIN_EXACT
    return score


def _model_relevance(match: str) -> str:
    """Translate legacy model_match into explicit base-model relevance.

    Compatibility value ``exact`` means exact equality to the model argument.
    Identity-aware discovery passes the base model, so ``exact`` must not be
    interpreted downstream as an exact product variant.
    """
    return {
        "exact": "exact_base_model",
        "likely_variant": "variant_of_base_model",
        "likely": "probable_base_model",
        "mismatch": "different_model",
        "different_variant": "different_model",
        "unknown": "unknown",
    }[match]


def _has_non_product_context(url: str, title: str) -> bool:
    """Detect generic person/sports/wiki/forum context without brand hardcoding."""
    domain = _host(url)
    if any(
        domain == item or domain.endswith(f".{item}")
        for item in NON_PRODUCT_CONTEXT_DOMAINS
    ):
        return True
    path_tokens = {
        token
        for segment in urlparse(url).path.split("/")
        for token in re.findall(r"[a-z0-9]+", segment.casefold())
    }
    text_tokens = set(normalize_text(title).split())
    return bool(
        path_tokens & NON_PRODUCT_CONTEXT_PATHS
        or text_tokens & NON_PRODUCT_CONTEXT_TERMS
    )


def _has_accessory_context(url: str, title: str) -> bool:
    """Reject accessories and replacement parts that merely name the product."""
    tokens = set(normalize_text(f"{title} {unquote(urlparse(url).path)}").split())
    return bool(tokens & ACCESSORY_CONTEXT_TERMS)


def _model_component_relation(model: str, text: str) -> RelevanceRelation | None:
    """Match compound commercial-model/MPN input across punctuation and prose."""
    expected = [
        normalize_model(part)
        for part in re.findall(r"[\w]+", model)
        if normalize_model(part)
    ]
    if len(expected) < 2:
        return None
    actual = {
        normalize_model(part)
        for part in re.findall(r"[\w]+", text)
        if normalize_model(part)
    }
    if all(part in actual for part in expected):
        return "exact"
    strong_identifiers = [
        part for part in expected
        if len(part) >= 5
        and any(character.isalpha() for character in part)
        and any(character.isdigit() for character in part)
    ]
    if any(part in actual for part in strong_identifiers):
        return "exact"
    matched = [part for part in expected if part in actual]
    if (
        len(matched) >= 2
        and any(any(character.isdigit() for character in part) for part in matched)
    ):
        return "weak"
    return None


def assess_candidate_relevance(
    candidate: Candidate,
    brand: str,
    model: str,
    article: str | None = None,
) -> tuple[RelevanceRelation, list[str]]:
    """Classify search-level product relevance before any network fetch.

    Authority is deliberately absent from the decision. A provider result must
    expose the requested model/article in its title/snippet or URL; a brand-only
    result cannot become relevant through source type or provider reputation.
    """
    title = str(candidate.get("title") or "")
    snippet = str(candidate.get("snippet") or "")
    url = str(candidate.get("url") or "")
    haystack = f"{title} {url}"
    match = str(candidate.get("model_match") or "unknown")
    exact_phrase = base_model_in_text(model, haystack)
    exact_article = article_matches(article, haystack)
    component_relation = _model_component_relation(model, haystack)

    if match == "different_variant":
        return "reject", ["Search result names a different commercial-model variant."]
    if (
        match == "mismatch"
        and not exact_phrase
        and not exact_article
        and component_relation is None
    ):
        return "reject", ["Search result contains a conflicting model identifier."]
    if _has_non_product_context(url, title):
        return "reject", ["Search result has person, sports, profile, wiki, or forum context."]
    if _has_accessory_context(url, title):
        return "reject", ["Search result describes an accessory or replacement part, not the product."]
    if match == "likely_variant":
        return "likely_variant", ["Requested base model appears with an explicit variant suffix."]
    if match == "exact" or exact_phrase or component_relation == "exact":
        if _page_kind(url) in {"catalog", "homepage", "weak"}:
            return "weak", ["Exact model appears only on a generic catalog or weak page."]
        return "exact", ["Requested exact model appears in the title/snippet or URL."]
    if exact_article:
        return "exact", ["Requested article/MPN appears in the title/snippet or URL."]
    if match == "likely":
        return "weak", ["Search result contains only a probable base-model relation."]
    if component_relation == "weak":
        return "weak", ["Search result contains a distinctive partial commercial-model relation."]

    # Some search providers redact titles/snippets for first-party support
    # pages (for example returning only "Brand Support").  Keep exactly one
    # such result eligible for a bounded content-verification fetch, but only
    # when it came from an identity-bearing query and authority was already
    # independently verified.  It remains weak/unknown until fetched content
    # proves the model, so query text can never become product evidence.
    provenance = list(candidate.get("discovery_provenance") or ())
    exact_query = any(
        base_model_in_text(model, str(item.get("query") or ""))
        for item in provenance
        if isinstance(item, Mapping)
    )
    if (
        match == "unknown"
        and candidate.get("authority_status") == "verified"
        and candidate.get("source_type") in {"manufacturer", "official_document"}
        and exact_query
        and _page_kind(url) not in {"homepage", "catalog", "weak"}
    ):
        return "weak", [
            "Verified first-party result from an exact-model query requires content verification."
        ]

    brand_present = normalize_model(brand) in normalize_model(f"{haystack} {snippet}")
    reason = "Requested exact model/article is absent from the search result."
    if brand_present:
        reason += " Brand-only evidence is insufficient."
    return "reject", [reason]


def _partition_relevance_candidates(
    candidates: Iterable[Candidate],
    brand: str,
    model: str,
    article: str | None,
) -> tuple[list[Candidate], list[Candidate]]:
    accepted: list[Candidate] = []
    rejected: list[Candidate] = []
    for candidate in candidates:
        relation, reasons = assess_candidate_relevance(
            candidate, brand, model, article,
        )
        candidate["relevance_relation"] = relation
        candidate["relevance_reasons"] = reasons
        (rejected if relation == "reject" else accepted).append(candidate)
    accepted.sort(key=lambda item: (-item["score"], item["url"]))
    rejected.sort(key=lambda item: (-item["score"], item["url"]))
    return accepted, rejected


def rank_candidates(results: Iterable[SearchResultLike], brand: str, model: str,
                    article: str | None = None, official_domain: str | None = None,
                    authority_evidence_url: str | None = None,
                    market: str = "global",
                    official_domains: dict[str, str] | None = None) -> list[Candidate]:
    candidates: dict[str, Candidate] = {}
    for item in results:
        record = _search_result_record(item)
        raw_url = record.url
        title = record.title
        search_text = " ".join(part for part in (record.title, record.snippet) if part)
        url = canonicalize_url(raw_url)
        if not url or is_obvious_non_product_url(url):
            continue
        domain = _host(url)
        matched_official_domain = next(
            (item for item in (official_domains or {})
             if domain == item or domain.endswith(f".{item}")),
            official_domain if official_domain and (domain == official_domain or domain.endswith(f".{official_domain}")) else None,
        )
        source_type = classify_source(domain, matched_official_domain)
        authority_status = "unknown"
        evidence_url = None
        authority_reason = None
        candidate_evidence = (official_domains or {}).get(matched_official_domain or "", authority_evidence_url)
        if source_type == "manufacturer" and candidate_evidence:
            authority_status = "verified"
            evidence_url = candidate_evidence
            authority_reason = "Domain verified from conservative brand official-search evidence."
            if _is_official_document(url):
                source_type = "official_document"
        # Provider snippets are useful ranking context but are not identity
        # evidence: they often echo the query beside a different SKU.
        match = candidate_model_match(model, record.title, urlparse(url).path)
        product_match_evidence = None
        if match == "exact":
            product_match_evidence = "Exact normalized model token found in title/snippet or URL."
        elif match == "likely_variant":
            product_match_evidence = "Base model found with an explicit variant suffix."
        elif match == "likely":
            product_match_evidence = "Model-like identifier extends the requested base model."
        elif article_matches(article, f"{search_text} {url}"):
            product_match_evidence = "Exact article/MPN token found in title/snippet or URL."
        candidate: Candidate = {
            "url": url, "domain": domain, "title": " ".join((title or "").split()),
            "source_type": source_type, "authority_status": authority_status,
            "authority_evidence_url": evidence_url, "authority_reason": authority_reason,
            "product_match_evidence": product_match_evidence,
            "market_scope": "unknown",
            "model_match": match,
            "model_relevance": _model_relevance(match),
            "score": score_candidate(
                url, search_text, source_type, authority_status, match, model, article, brand,
            ),
            "identity_relation": "unknown",
            "identity_verification_evidence": [],
            "relevance_relation": "reject",
            "relevance_reasons": [],
            "snippet": " ".join((record.snippet or "").split()),
            "discovery_provider": record.provider,
            "discovery_query": record.query,
            "discovery_rank": record.rank,
            "raw_url": record.raw_url or raw_url,
            "redirect_url": record.redirect_url,
            "parse_status": record.parse_status,
            "parse_confidence": record.parse_confidence,
            "discovery_provenance": [{
                "provider": record.provider,
                "query": record.query,
                "rank": record.rank,
                "raw_url": record.raw_url or raw_url,
                "redirect_url": record.redirect_url,
                "parse_status": record.parse_status,
                "parse_confidence": record.parse_confidence,
            }],
        }
        previous = candidates.get(url)
        if previous is None:
            candidates[url] = candidate
            continue
        merged_provenance = list(previous.get("discovery_provenance") or ())
        for provenance in candidate["discovery_provenance"]:
            if provenance not in merged_provenance:
                merged_provenance.append(provenance)
        winner = candidate if candidate["score"] > previous["score"] else previous
        winner["discovery_provenance"] = merged_provenance
        candidates[url] = winner
    return sorted(candidates.values(), key=lambda item: (-item["score"], item["url"]))


class _GoogleParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.results: list[SearchResult] = []
        self._href = ""
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            self._href = dict(attrs).get("href") or ""
            self._text = []

    def handle_data(self, data: str) -> None:
        if self._href:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag != "a" or not self._href:
            return
        href = _clean_google_result_url(self._href)
        if href.startswith(("http://", "https://")):
            self.results.append((href, " ".join(self._text).strip()))
        self._href = ""
        self._text = []


INTERSTITIAL_MARKERS = (
    "before you continue to google", "consent.google.com", "enable javascript",
    "our systems have detected unusual traffic", "unusual traffic",
    "not a robot", "recaptcha", "необычный трафик", "не робот",
)


def _is_external_result(url: str) -> bool:
    host = _host(url)
    return bool(host) and not (
        host == "google.com" or host.endswith(".google.com")
        or host == "googleusercontent.com" or host.endswith(".googleusercontent.com")
    )


def _clean_google_result_url(raw_url: str) -> str:
    url = unquote((raw_url or "").strip())
    for _ in range(3):
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        google_redirect = (
            parsed.path in {"/url", "/goto"}
            and (
                not host
                or host == "google.com"
                or host.endswith(".google.com")
            )
        )
        if not google_redirect:
            break
        query = parse_qs(parsed.query)
        target = next((
            values[0]
            for key in ("q", "url", "u", "target")
            if (values := query.get(key)) and values[0]
        ), "")
        if not target or target == url:
            return ""
        url = unquote(target)
    return canonicalize_url(unquote(url))


def _parse_google_browser_items(
    items: Iterable[Mapping[str, object]],
) -> list[SearchResultRecord]:
    """Normalize several generic Google result-link layouts into one contract."""
    found: list[SearchResultRecord] = []
    seen: set[str] = set()
    for position, item in enumerate(items, start=1):
        raw_urls = [
            str(item.get(name) or "").strip()
            for name in ("href", "dataHref", "dataUrl", "pingTarget")
        ]
        raw_url = next((value for value in raw_urls if value), "")
        url = next((
            cleaned for value in raw_urls
            if (cleaned := _clean_google_result_url(value)) and _is_external_result(cleaned)
        ), "")
        if not url or url in seen:
            continue
        seen.add(url)
        title = " ".join(str(item.get("title") or "").split())
        block_text = " ".join(str(item.get("text") or "").split())
        if not title:
            title = block_text.split("\n", 1)[0].strip()
        snippet = block_text
        if title and snippet.casefold().startswith(title.casefold()):
            snippet = snippet[len(title):].strip(" -—|\n")
        found.append(SearchResultRecord(
            url=url,
            title=title or block_text,
            snippet=snippet,
            rank=position,
            raw_url=raw_url,
            redirect_url=raw_url if raw_url and raw_url != url else None,
            parse_status="parsed",
            parse_confidence="high" if title else "medium",
        ))
    return found


def _clean_bing_result_url(raw_url: str) -> str:
    """Resolve Bing's optional ``/ck/a`` wrapper without following it."""
    url = unquote((raw_url or "").strip())
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host == "bing.com" or host.endswith(".bing.com"):
        encoded = next(iter(parse_qs(parsed.query).get("u", ())), "")
        if not encoded.startswith("a1"):
            return ""
        payload = encoded[2:]
        try:
            padding = "=" * (-len(payload) % 4)
            url = base64.urlsafe_b64decode(payload + padding).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return ""
    return canonicalize_url(url)


class _BingParser(HTMLParser):
    """Parse organic Bing results while ignoring navigation/answer links."""

    def __init__(self) -> None:
        super().__init__()
        self.results: list[SearchResultRecord] = []
        self.raw_result_count = 0
        self._result_depth = 0
        self._in_heading = False
        self._capture_href = ""
        self._capture_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        classes = set((values.get("class") or "").split())
        if not self._result_depth and tag == "li" and "b_algo" in classes:
            self._result_depth = 1
            return
        if not self._result_depth:
            return
        self._result_depth += 1
        if tag == "h2":
            self._in_heading = True
        elif tag == "a" and self._in_heading and not self._capture_href:
            self._capture_href = values.get("href") or ""
            self._capture_text = []

    def handle_data(self, data: str) -> None:
        if self._capture_href:
            self._capture_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if not self._result_depth:
            return
        if tag == "a" and self._capture_href:
            self.raw_result_count += 1
            raw_url = self._capture_href
            url = _clean_bing_result_url(raw_url)
            title = " ".join(" ".join(self._capture_text).split())
            if url and _is_external_result(url):
                self.results.append(SearchResultRecord(
                    url=url,
                    title=title,
                    provider="bing",
                    rank=len(self.results) + 1,
                    raw_url=raw_url,
                    redirect_url=raw_url if raw_url != url else None,
                    parse_status="parsed",
                    parse_confidence="high" if title else "medium",
                ))
            self._capture_href = ""
            self._capture_text = []
        if tag == "h2":
            self._in_heading = False
        self._result_depth -= 1


BING_MARKETS = {
    "DE": "de-DE",
    "GB": "en-GB",
    "RU": "ru-RU",
    "US": "en-US",
    "global": "en-US",
}


class BingSearchProvider:
    """Independent keyless HTML provider with bounded request time."""

    name = "bing"
    quality_gate = True

    def __init__(self, market: str = "global", *, timeout_seconds: float = 8.0) -> None:
        if market not in SUPPORTED_MARKETS:
            raise ValueError(f"Unsupported market: {market}")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.market = market
        self.timeout_seconds = float(timeout_seconds)
        self.last_raw_result_count = 0
        self.last_transport = "http"

    def search(self, query: str) -> list[SearchResultRecord]:
        return self.search_with_timeout(query, self.timeout_seconds)

    def search_with_timeout(
        self,
        query: str,
        timeout_seconds: float,
    ) -> list[SearchResultRecord]:
        params = {
            "q": query,
            "count": "10",
            "mkt": BING_MARKETS[self.market],
            "setlang": BING_MARKETS[self.market].split("-", 1)[0],
        }
        try:
            response = requests.get(
                f"https://www.bing.com/search?{urlencode(params)}",
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/131.0.0.0 Safari/537.36"
                    ),
                    "Accept-Language": "en-US,en;q=0.8",
                },
                timeout=max(0.1, min(20.0, timeout_seconds)),
            )
            response.raise_for_status()
        except requests.Timeout as error:
            raise ProviderTimeoutError("Bing provider deadline exhausted.") from error
        except requests.RequestException as error:
            raise RuntimeError(f"Bing search failed: {error}") from error
        parser = _BingParser()
        parser.feed(response.text)
        self.last_raw_result_count = parser.raw_result_count
        lowered = response.text.lower()
        if not parser.results and any(marker in lowered for marker in (
            "captcha", "verify you are a human", "unusual traffic",
        )):
            raise RuntimeError("Bing bot-check blocked the search.")
        if parser.raw_result_count and not parser.results:
            raise ProviderParseError(
                "Bing returned organic results but none could be parsed."
            )
        return parser.results


def _google_result_layout_ready(page: object) -> bool:
    """Recognize result headings even when the anchor wraps a parent/sibling."""
    locator = getattr(page, "locator")
    return bool(locator("h3").count() or locator("a:has(h3)").count())


def needs_playwright_fallback(results: Iterable[SearchResultLike], html: str) -> bool:
    """Decide whether the HTTP response contains usable organic results."""
    text = (html or "").lower()
    external = [
        record.url
        for item in results
        if _is_external_result((record := _search_result_record(item)).url)
    ]
    return not external or any(marker in text for marker in INTERSTITIAL_MARKERS)


def _http_google_search(query: str) -> tuple[list[SearchResult], str]:
    return _http_google_search_for_market(query, "global")


def _google_search_url(query: str, market: str) -> str:
    if market not in SUPPORTED_MARKETS:
        raise ValueError(f"Unsupported market: {market}")
    params = {"q": query, "num": "10", **MARKET_GOOGLE_PARAMS[market]}
    return f"https://www.google.com/search?{urlencode(params)}"


def _http_google_search_for_market(
    query: str,
    market: str,
    *,
    timeout: float = 15.0,
) -> tuple[list[SearchResult], str]:
    request = Request(
        _google_search_url(query, market),
        headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "en-US,en;q=0.8"},
    )
    try:
        with urlopen(request, timeout=max(0.1, timeout)) as response:
            html = response.read().decode("utf-8", errors="replace")
    except Exception:
        return [], ""
    parser = _GoogleParser()
    parser.feed(html)
    return [(url, title) for url, title in parser.results if _is_external_result(url)], html


class GoogleSearchSession:
    """HTTP-first Google search with one lazy Playwright browser per discovery."""

    name = "google"
    quality_gate = True

    def __init__(self, market: str = "global", *, timeout_seconds: float = 25.0) -> None:
        if market not in SUPPORTED_MARKETS:
            raise ValueError(f"Unsupported market: {market}")
        self.market = market
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.timeout_seconds = float(timeout_seconds)
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        self.last_transport: str | None = None

    def __enter__(self) -> "GoogleSearchSession":
        return self

    def __exit__(self, *_: object) -> None:
        self.release_transient_resources()

    def release_transient_resources(self) -> None:
        """Close the lazy browser lifecycle while keeping provider state reusable."""
        context = self._context
        browser = self._browser
        playwright = self._playwright
        self._page = None
        self._context = None
        self._browser = None
        self._playwright = None
        try:
            if context is not None:
                context.close()
        finally:
            try:
                if browser is not None:
                    browser.close()
            finally:
                if playwright is not None:
                    playwright.stop()

    def search(self, query: str) -> list[SearchResultLike]:
        return self.search_with_timeout(query, self.timeout_seconds)

    def search_with_timeout(self, query: str, timeout_seconds: float) -> list[SearchResultLike]:
        deadline = time.monotonic() + max(0.1, timeout_seconds)
        self.last_transport = "http"
        results, html = _http_google_search_for_market(
            query,
            self.market,
            timeout=min(15.0, max(0.1, deadline - time.monotonic())),
        )
        if not needs_playwright_fallback(results, html):
            return results
        if time.monotonic() >= deadline:
            raise ProviderTimeoutError("Google provider deadline exhausted after HTTP search.")
        self.last_transport = "browser"
        return self._playwright_search(query, deadline=deadline)

    def _ensure_page(self, *, deadline: float | None = None):
        if self._page is not None:
            return self._page
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as error:
            raise RuntimeError(
                "Google HTTP search did not return organic results. Install Playwright "
                "with 'pip install -r requirements.txt' and 'playwright install chromium'."
            ) from error
        self._playwright = sync_playwright().start()
        try:
            profile_dir = Path(tempfile.gettempdir()) / "product-data-verifier-google-profile"
            launch_timeout = (
                max(1, int((deadline - time.monotonic()) * 1000))
                if deadline is not None else 30000
            )
            if launch_timeout <= 1 and deadline is not None:
                raise ProviderTimeoutError(
                    "Google provider deadline exhausted before browser launch."
                )
            self._context = self._playwright.chromium.launch_persistent_context(
                str(profile_dir),
                headless=browser_headless(),
                args=["--disable-blink-features=AutomationControlled"],
                locale="en-US",
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1440, "height": 1000},
                timeout=launch_timeout,
            )
        except ProviderTimeoutError:
            self._playwright.stop()
            self._playwright = None
            raise
        except Exception as error:
            self._playwright.stop()
            self._playwright = None
            raise RuntimeError(
                "Playwright Chromium is unavailable. Run 'playwright install chromium'."
            ) from error
        self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
        return self._page

    @staticmethod
    def _accept_consent(page, *, deadline: float | None = None) -> bool:
        for label in ("Accept all", "I agree", "Accept", "Принять все", "Согласен"):
            try:
                button = page.get_by_text(label, exact=True)
                if button.count():
                    timeout = 3000
                    if deadline is not None:
                        timeout = max(1, min(timeout, int(
                            max(0.0, deadline - time.monotonic()) * 1000
                        )))
                    button.first.click(timeout=timeout)
                    remaining = (
                        800 if deadline is None else max(
                            0, min(800, int((deadline - time.monotonic()) * 1000)),
                        )
                    )
                    if remaining:
                        page.wait_for_timeout(remaining)
                    return True
            except Exception:
                continue
        return False

    def _playwright_search(
        self,
        query: str,
        *,
        deadline: float | None = None,
    ) -> list[SearchResultLike]:
        deadline = deadline or (time.monotonic() + self.timeout_seconds)
        page = self._ensure_page(deadline=deadline)
        search_url = _google_search_url(query, self.market)

        def remaining_ms(maximum: int) -> int:
            remaining = int(max(0.0, deadline - time.monotonic()) * 1000)
            if remaining <= 0:
                raise ProviderTimeoutError("Google provider deadline exhausted.")
            return max(1, min(maximum, remaining))

        try:
            page.goto(search_url, wait_until="domcontentloaded", timeout=remaining_ms(60000))
            self._accept_consent(page, deadline=deadline)
            page.wait_for_load_state("domcontentloaded", timeout=remaining_ms(10000))
        except ProviderTimeoutError:
            raise
        except Exception as error:
            if time.monotonic() >= deadline:
                raise ProviderTimeoutError("Google provider deadline exhausted while loading.") from error
            raise RuntimeError(f"Playwright could not load Google search: {error}") from error

        body_text = ""
        while time.monotonic() < deadline:
            try:
                body_text = page.locator("body").inner_text(timeout=remaining_ms(3000)).lower()
                if _google_result_layout_ready(page):
                    break
                if any(marker in body_text for marker in INTERSTITIAL_MARKERS):
                    self._accept_consent(page, deadline=deadline)
            except Exception:
                pass
            if time.monotonic() < deadline:
                page.wait_for_timeout(min(500, remaining_ms(500)))

        if time.monotonic() >= deadline and not _google_result_layout_ready(page):
            raise ProviderTimeoutError("Google provider deadline exhausted waiting for results.")

        if any(marker in body_text for marker in (
            "unusual traffic", "not a robot", "recaptcha", "необычный трафик", "не робот",
        )):
            raise RuntimeError("Google bot-check blocked the Playwright search.")

        try:
            items = page.locator("body").evaluate(
                """body => {
                    const found = [];
                    const seen = new Set();
                    const add = (anchor, heading, block) => {
                        if (!anchor) return;
                        const href = anchor.getAttribute('href') || anchor.href || '';
                        const dataHref = anchor.getAttribute('data-href') || '';
                        const dataUrl = anchor.getAttribute('data-url') || '';
                        const key = [href, dataHref, dataUrl, (heading && heading.innerText) || ''].join('|');
                        if (seen.has(key)) return;
                        seen.add(key);
                        found.push({
                            href,
                            dataHref,
                            dataUrl,
                            pingTarget: anchor.getAttribute('ping') || '',
                            title: ((heading && heading.innerText) || anchor.innerText || '').trim(),
                            text: ((block && block.innerText) || anchor.innerText || '').trim()
                        });
                    };
                    for (const heading of body.querySelectorAll('h3')) {
                        let block = heading.closest('.MjjYud, .g, [data-snhf], [data-ved]');
                        let anchor = heading.closest('a[href], a[data-href], a[data-url]');
                        if (!anchor && block) {
                            anchor = block.querySelector('a[href], a[data-href], a[data-url]');
                        }
                        if (!block) {
                            let node = heading.parentElement;
                            for (let i = 0; i < 7 && node; i++, node = node.parentElement) {
                                const candidate = node.querySelector &&
                                    node.querySelector('a[href], a[data-href], a[data-url]');
                                if (candidate) { anchor = anchor || candidate; block = node; break; }
                            }
                        }
                        add(anchor, heading, block);
                    }
                    for (const anchor of body.querySelectorAll('a[href], a[data-href], a[data-url]')) {
                        const heading = anchor.querySelector('h3');
                        if (!heading) continue;
                        add(anchor, heading, anchor.closest('.MjjYud, .g, [data-snhf], [data-ved]'));
                    }
                    return found;
                }"""
            )
        except Exception as error:
            raise ProviderParseError(f"Could not extract Google organic results: {error}") from error

        for item in items:
            raw_url = str(item.get("href") or "")
            if raw_url.startswith("/goto?"):
                try:
                    response = self._context.request.get(
                        f"https://www.google.com{raw_url}", timeout=remaining_ms(15000)
                    )
                    item["href"] = response.url
                except Exception:
                    continue
        found = _parse_google_browser_items(items)
        if not found and (
            page.locator("h3").count()
            or "search results" in body_text
            or "web results" in body_text
        ):
            raise ProviderParseError(
                "Google returned a normal result page but no external result link could be parsed."
            )
        return found


class _DuckDuckGoLiteParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.results: list[SearchResult] = []
        self._href = ""
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        classes = set((values.get("class") or "").split())
        if tag == "a" and "result-link" in classes:
            self._href = values.get("href") or ""
            self._text = []

    def handle_data(self, data: str) -> None:
        if self._href:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag != "a" or not self._href:
            return
        url = _clean_duckduckgo_result_url(self._href)
        if url:
            self.results.append((url, " ".join(self._text).strip()))
        self._href = ""
        self._text = []


DUCKDUCKGO_MARKETS = {
    "US": "us-en",
    "GB": "uk-en",
    "DE": "de-de",
    "RU": "ru-ru",
}
DUCKDUCKGO_BLOCK_MARKERS = (
    "challenge-form",
    "anomaly-modal",
    "bots use duckduckgo too",
    "verify you are a human",
)
DUCKDUCKGO_MIN_INTERVAL_SECONDS = 2.0
_DUCKDUCKGO_LAST_REQUEST_AT = 0.0


def _clean_duckduckgo_result_url(raw_url: str) -> str:
    url = (raw_url or "").strip()
    if url.startswith("//"):
        url = f"https:{url}"
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host == "duckduckgo.com" or host.endswith(".duckduckgo.com"):
        target = (parse_qs(parsed.query).get("uddg") or [""])[0]
        return canonicalize_url(unquote(target))
    return canonicalize_url(url)


class _DuckDuckGoHtmlParser(HTMLParser):
    """Parse organic results from both current DDG HTML result layouts."""

    _TITLE_CLASSES = {"result__a", "result-link", "result-title-a"}
    _SNIPPET_CLASSES = {"result__snippet", "result-snippet"}

    def __init__(self) -> None:
        super().__init__()
        self.raw_result_count = 0
        self._entries: list[dict[str, str]] = []
        self._capture: str | None = None
        self._capture_depth = 0
        self._capture_text: list[str] = []
        self._capture_href = ""
        self._snippet_target = -1

    @property
    def results(self) -> list[SearchResultRecord]:
        return [
            SearchResultRecord(
                url=item["url"],
                title=item["title"],
                snippet=item.get("snippet", ""),
                raw_url=item["raw_url"],
                redirect_url=(
                    item["raw_url"] if item["raw_url"] != item["url"] else None
                ),
                parse_status="parsed",
                parse_confidence="high" if item["title"] else "medium",
            )
            for item in self._entries
        ]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._capture is not None:
            self._capture_depth += 1
            return
        values = dict(attrs)
        classes = set((values.get("class") or "").split())
        if tag == "a" and classes & self._TITLE_CLASSES:
            self.raw_result_count += 1
            self._capture = "title"
            self._capture_depth = 1
            self._capture_text = []
            self._capture_href = values.get("href") or ""
            return
        if classes & self._SNIPPET_CLASSES and self._entries:
            self._capture = "snippet"
            self._capture_depth = 1
            self._capture_text = []
            self._snippet_target = len(self._entries) - 1

    def handle_data(self, data: str) -> None:
        if self._capture is not None:
            self._capture_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self._capture is None:
            return
        self._capture_depth -= 1
        if self._capture_depth > 0:
            return
        text = " ".join(" ".join(self._capture_text).split())
        if self._capture == "title":
            url = _clean_duckduckgo_result_url(self._capture_href)
            if url:
                self._entries.append({
                    "url": url,
                    "title": text,
                    "snippet": "",
                    "raw_url": self._capture_href,
                })
        elif 0 <= self._snippet_target < len(self._entries):
            self._entries[self._snippet_target]["snippet"] = text
        self._capture = None
        self._capture_depth = 0
        self._capture_text = []
        self._capture_href = ""
        self._snippet_target = -1


class DuckDuckGoHtmlSearchProvider:
    """Keyless DDG HTML route, independent from the frequently blocked Lite UI."""

    name = "duckduckgo_html"
    quality_gate = True

    def __init__(self, market: str = "global", *, timeout_seconds: float = 8.0) -> None:
        if market not in SUPPORTED_MARKETS:
            raise ValueError(f"Unsupported market: {market}")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.market = market
        self.timeout_seconds = float(timeout_seconds)
        self.last_raw_result_count = 0
        self.last_transport = "http"

    def search(self, query: str) -> list[SearchResultRecord]:
        return self.search_with_timeout(query, self.timeout_seconds)

    def search_with_timeout(
        self,
        query: str,
        timeout_seconds: float,
    ) -> list[SearchResultRecord]:
        deadline = time.monotonic() + max(0.1, timeout_seconds)
        global _DUCKDUCKGO_LAST_REQUEST_AT
        remaining = DUCKDUCKGO_MIN_INTERVAL_SECONDS - (
            time.monotonic() - _DUCKDUCKGO_LAST_REQUEST_AT
        )
        if remaining > 0:
            if remaining >= timeout_seconds:
                raise ProviderTimeoutError(
                    "DuckDuckGo HTML provider deadline exhausted during rate limit."
                )
            time.sleep(remaining)
        params = {"q": query}
        region = DUCKDUCKGO_MARKETS.get(self.market)
        if region:
            params["kl"] = region
        url = f"https://html.duckduckgo.com/html/?{urlencode(params)}"
        headers = {
            "User-Agent": "Mozilla/5.0",
            "Accept-Language": "en-US,en;q=0.8",
        }
        try:
            _DUCKDUCKGO_LAST_REQUEST_AT = time.monotonic()
            remaining_timeout = deadline - time.monotonic()
            if remaining_timeout <= 0:
                raise ProviderTimeoutError(
                    "DuckDuckGo HTML provider deadline exhausted before request."
                )
            response = requests.get(
                url,
                headers=headers,
                timeout=max(0.1, min(20.0, remaining_timeout)),
            )
            response.raise_for_status()
            html = response.text
        except ProviderTimeoutError:
            raise
        except requests.Timeout as error:
            raise ProviderTimeoutError(
                "DuckDuckGo HTML provider deadline exhausted."
            ) from error
        except requests.RequestException as error:
            raise RuntimeError(f"DuckDuckGo HTML search failed: {error}") from error
        except Exception as error:
            if time.monotonic() >= deadline:
                raise ProviderTimeoutError(
                    "DuckDuckGo HTML provider deadline exhausted."
                ) from error
            raise RuntimeError(f"DuckDuckGo HTML search failed: {error}") from error
        lowered = html.lower()
        if any(marker in lowered for marker in DUCKDUCKGO_BLOCK_MARKERS):
            raise RuntimeError("DuckDuckGo bot-check blocked the HTML search.")
        parser = _DuckDuckGoHtmlParser()
        parser.feed(html)
        self.last_raw_result_count = parser.raw_result_count
        if parser.raw_result_count and not parser.results:
            raise ProviderParseError(
                "DuckDuckGo HTML returned organic results but none could be parsed."
            )
        return parser.results


class DuckDuckGoLiteSearchProvider:
    """Keyless HTML fallback with the same URL/title result contract."""

    name = "duckduckgo_lite"
    quality_gate = True

    def __init__(self, market: str = "global", *, timeout_seconds: float = 8.0) -> None:
        if market not in SUPPORTED_MARKETS:
            raise ValueError(f"Unsupported market: {market}")
        self.market = market
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.timeout_seconds = float(timeout_seconds)
        self.last_transport = "http"
        self.last_raw_result_count = 0

    def search(self, query: str) -> list[SearchResult]:
        return self.search_with_timeout(query, self.timeout_seconds)

    def search_with_timeout(self, query: str, timeout_seconds: float) -> list[SearchResult]:
        deadline = time.monotonic() + max(0.1, timeout_seconds)
        global _DUCKDUCKGO_LAST_REQUEST_AT
        remaining = DUCKDUCKGO_MIN_INTERVAL_SECONDS - (
            time.monotonic() - _DUCKDUCKGO_LAST_REQUEST_AT
        )
        if remaining > 0:
            if remaining >= timeout_seconds:
                raise ProviderTimeoutError("DuckDuckGo provider deadline exhausted during rate limit.")
            time.sleep(remaining)
        params = {"q": query}
        region = DUCKDUCKGO_MARKETS.get(self.market)
        if region:
            params["kl"] = region
        request = Request(
            f"https://lite.duckduckgo.com/lite/?{urlencode(params)}",
            headers={
                "User-Agent": "Mozilla/5.0",
                "Accept-Language": "en-US,en;q=0.8",
            },
        )
        try:
            _DUCKDUCKGO_LAST_REQUEST_AT = time.monotonic()
            remaining_timeout = deadline - time.monotonic()
            if remaining_timeout <= 0:
                raise ProviderTimeoutError("DuckDuckGo provider deadline exhausted before request.")
            with urlopen(request, timeout=max(0.1, min(20.0, remaining_timeout))) as response:
                html = response.read().decode("utf-8", errors="replace")
        except ProviderTimeoutError:
            raise
        except Exception as error:
            if time.monotonic() >= deadline:
                raise ProviderTimeoutError("DuckDuckGo provider deadline exhausted.") from error
            raise RuntimeError(f"DuckDuckGo Lite search failed: {error}") from error
        lowered = html.lower()
        if any(marker in lowered for marker in DUCKDUCKGO_BLOCK_MARKERS):
            raise RuntimeError("DuckDuckGo bot-check blocked the Lite search.")
        parser = _DuckDuckGoLiteParser()
        parser.feed(html)
        self.last_raw_result_count = len(parser.results)
        return parser.results


class _NaverParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.results: list[SearchResult] = []
        self._href = ""
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        classes = set((values.get("class") or "").split())
        href = values.get("href") or ""
        if tag != "a" or "fds-anchor-layout" not in classes:
            return
        if not href.startswith(("http://", "https://")):
            return
        host = _host(href)
        if host == "naver.com" or host.endswith(".naver.com"):
            return
        self._href = href
        self._text = []

    def handle_data(self, data: str) -> None:
        if self._href:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag != "a" or not self._href:
            return
        url = canonicalize_url(self._href)
        if url:
            self.results.append((url, " ".join(self._text).strip()))
        self._href = ""
        self._text = []


class NaverSearchProvider:
    """Second keyless fallback for resilient public web discovery."""

    name = "naver"
    quality_gate = True

    def __init__(self, market: str = "global", *, timeout_seconds: float = 8.0) -> None:
        if market not in SUPPORTED_MARKETS:
            raise ValueError(f"Unsupported market: {market}")
        self.market = market
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.timeout_seconds = float(timeout_seconds)
        self.last_transport = "http"
        self.last_raw_result_count = 0

    def search(self, query: str) -> list[SearchResult]:
        return self.search_with_timeout(query, self.timeout_seconds)

    def search_with_timeout(self, query: str, timeout_seconds: float) -> list[SearchResult]:
        deadline = time.monotonic() + max(0.1, timeout_seconds)
        request = Request(
            f"https://search.naver.com/search.naver?{urlencode({'where': 'web', 'query': query})}",
            headers={
                "User-Agent": "Mozilla/5.0",
                "Accept-Language": "en-US,en;q=0.8",
            },
        )
        try:
            remaining_timeout = deadline - time.monotonic()
            if remaining_timeout <= 0:
                raise ProviderTimeoutError("Naver provider deadline exhausted before request.")
            with urlopen(request, timeout=max(0.1, min(20.0, remaining_timeout))) as response:
                html = response.read().decode("utf-8", errors="replace")
        except ProviderTimeoutError:
            raise
        except Exception as error:
            if time.monotonic() >= deadline:
                raise ProviderTimeoutError("Naver provider deadline exhausted.") from error
            raise RuntimeError(f"Naver search failed: {error}") from error
        parser = _NaverParser()
        parser.feed(html)
        self.last_raw_result_count = len(parser.results)
        lowered = html.lower()
        if not parser.results and any(marker in lowered for marker in (
            "captcha", "verify you are a human", "비정상적인 접근",
        )):
            raise RuntimeError("Naver bot-check blocked the search.")
        return parser.results


class _SeznamParser(HTMLParser):
    """Parse external links; query-quality filtering removes navigation noise."""

    def __init__(self) -> None:
        super().__init__()
        self.results: list[SearchResultRecord] = []
        self.raw_result_count = 0
        self._href = ""
        self._text: list[str] = []
        self._seen: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        href = dict(attrs).get("href") or ""
        if not href.startswith(("http://", "https://")):
            return
        host = _host(href)
        if host == "seznam.cz" or host.endswith(".seznam.cz"):
            return
        self._href = href
        self._text = []

    def handle_data(self, data: str) -> None:
        if self._href:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag != "a" or not self._href:
            return
        self.raw_result_count += 1
        url = canonicalize_url(self._href)
        title = " ".join(" ".join(self._text).split())
        if url and title and url not in self._seen and not is_obvious_non_product_url(url):
            self._seen.add(url)
            self.results.append(SearchResultRecord(
                url=url,
                title=title,
                provider="seznam",
                rank=len(self.results) + 1,
                raw_url=self._href,
                parse_confidence="medium",
            ))
        self._href = ""
        self._text = []


class SeznamSearchProvider:
    """Independent keyless public index used when earlier providers fail."""

    name = "seznam"
    quality_gate = True

    def __init__(self, market: str = "global", *, timeout_seconds: float = 8.0) -> None:
        if market not in SUPPORTED_MARKETS:
            raise ValueError(f"Unsupported market: {market}")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.market = market
        self.timeout_seconds = float(timeout_seconds)
        self.last_transport = "http"
        self.last_raw_result_count = 0

    def search(self, query: str) -> list[SearchResultRecord]:
        return self.search_with_timeout(query, self.timeout_seconds)

    def search_with_timeout(
        self, query: str, timeout_seconds: float,
    ) -> list[SearchResultRecord]:
        try:
            response = requests.get(
                f"https://search.seznam.cz/?{urlencode({'q': query})}",
                headers={
                    "User-Agent": "Mozilla/5.0",
                    "Accept-Language": "en-US,en;q=0.8",
                },
                timeout=max(0.1, min(20.0, timeout_seconds)),
            )
            response.raise_for_status()
        except requests.Timeout as error:
            raise ProviderTimeoutError("Seznam provider deadline exhausted.") from error
        except requests.RequestException as error:
            raise RuntimeError(f"Seznam search failed: {error}") from error
        parser = _SeznamParser()
        parser.feed(response.text)
        self.last_raw_result_count = parser.raw_result_count
        lowered = response.text.lower()
        if not parser.results and any(marker in lowered for marker in (
            "captcha", "verify you are a human", "robot",
        )):
            raise RuntimeError("Seznam bot-check blocked the search.")
        return parser.results[:20]


_MARKET_ROOT_TLDS: dict[str, str] = {
    "DE": "de", "GB": "co.uk", "RU": "ru",
}

_TITLE_PATTERN = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_SITEMAP_LOC_PATTERN = re.compile(
    r"<loc\b[^>]*>(.*?)</loc>", re.IGNORECASE | re.DOTALL,
)


def _extract_page_title(html: str) -> str:
    match = _TITLE_PATTERN.search(html or "")
    if not match:
        return ""
    return re.sub(r"\s+", " ", unescape(match.group(1))).strip()


class _DirectLinkParser(HTMLParser):
    """Collect ordinary links from one manufacturer page without crawling it."""

    def __init__(self) -> None:
        super().__init__()
        self.links: list[tuple[str, str]] = []
        self._href = ""
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        self._href = dict(attrs).get("href") or ""
        self._text = []

    def handle_data(self, data: str) -> None:
        if self._href:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag != "a" or not self._href:
            return
        self.links.append((self._href, " ".join(" ".join(self._text).split())))
        self._href = ""
        self._text = []


class DirectDomainProbeProvider:
    """Independent discovery path: probe a likely official domain directly.

    Every other provider in this session's default tuple is a keyless
    HTML-scrape of a third-party search engine. Stage 24 (2026-09-17)
    observed all of them WAF-blocked or timing out within the same
    live run, which starved discovery for every remaining product in that
    run even though the manufacturer's own site was, in principle, directly
    reachable. This provider shares no transport, host, or rate limit with
    any of them: it never queries a search engine at all, only the brand's
    own candidate root domain(s), over a small, bounded number of direct
    HTTP GETs.

    It is intentionally narrow. It only activates for the brand-root
    "{brand} official website" bootstrap query -- the same one
    ``discover_global_official_domains`` already consumes -- and a probed
    domain is never auto-trusted: it becomes a plain candidate result like
    any other provider's, which still has to clear the existing
    brand-in-title / exact-model-corroboration acceptance path in
    ``discover_global_official_domains`` before it can be promoted to
    verified official authority. This does not weaken authority, identity,
    or validation, and it carries no per-product/per-brand hardcoded URL --
    the candidate domain is derived generically from the brand string.
    """

    name = "direct_domain_probe"
    quality_gate = False
    always_run = True

    _QUERY_PATTERN = re.compile(r"^(.+?) official website$")

    def __init__(
        self,
        market: str = "global",
        *,
        timeout_seconds: float = 6.0,
        session: requests.Session | None = None,
    ) -> None:
        if market not in SUPPORTED_MARKETS:
            raise ValueError(f"Unsupported market: {market}")
        self.market = market
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.timeout_seconds = float(timeout_seconds)
        self._session = session or requests.Session()
        self.last_transport = "http_direct"
        self.last_raw_result_count = 0
        self._brand = ""
        self._model = ""
        self._records: tuple[SearchResultRecord, ...] | None = None
        self._request_count = 0
        self.max_requests = 8

    def configure_identity(self, brand: str, model: str) -> None:
        """Provide product context without embedding any product/domain table."""
        brand = " ".join((brand or "").split())
        model = " ".join((model or "").split())
        if (brand, model) != (self._brand, self._model):
            self._brand, self._model = brand, model
            self._records = None
            self._request_count = 0

    def _candidate_domains(self, brand: str) -> list[str]:
        slug = re.sub(r"[^a-z0-9]", "", brand.casefold())
        if len(slug) < 3:
            return []
        domains = [f"{slug}.com"]
        market_tld = _MARKET_ROOT_TLDS.get(self.market)
        if market_tld:
            domains.append(f"{slug}.{market_tld}")
        return domains

    def search(self, query: str) -> list[SearchResultLike]:
        return self.search_with_timeout(query, self.timeout_seconds)

    @staticmethod
    def _model_in_text(model: str, text: str) -> bool:
        if not model or not text:
            return False
        if model_match(model, unquote(text)) == "exact":
            return True
        compact_model = re.sub(r"[^a-z0-9]", "", model.casefold())
        compact_text = re.sub(r"[^a-z0-9]", "", unquote(text).casefold())
        return len(compact_model) >= 4 and compact_model in compact_text

    def _get(self, url: str, deadline: float) -> requests.Response | None:
        if self._request_count >= self.max_requests:
            return None
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        self._request_count += 1
        try:
            response = self._session.get(
                url,
                timeout=max(0.2, min(3.0, remaining)),
                allow_redirects=True,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36"
                    ),
                },
            )
        except requests.RequestException:
            return None
        self.last_raw_result_count += 1
        return response if response.status_code < 400 else None

    def _matching_links(
        self,
        html: str,
        base_url: str,
        domain: str,
    ) -> list[SearchResultRecord]:
        from urllib.parse import urljoin

        parser = _DirectLinkParser()
        parser.feed(html or "")
        records: list[SearchResultRecord] = []
        for raw_url, title in parser.links:
            url = canonicalize_url(urljoin(base_url, raw_url))
            if (
                not url
                or not url_belongs_to_domain(url, domain)
                or _page_kind(url) in {"homepage", "catalog"}
                or not self._model_in_text(self._model, f"{title} {url}")
            ):
                continue
            records.append(SearchResultRecord(
                url=url,
                title=title or unquote(urlparse(url).path).replace("-", " "),
                snippet="Direct manufacturer-site link.",
                provider=self.name,
                raw_url=raw_url,
                parse_confidence="high" if title else "medium",
            ))
        return records

    def _sitemap_records(
        self,
        sitemap_urls: Iterable[str],
        domain: str,
        deadline: float,
    ) -> list[SearchResultRecord]:
        queue = list(dict.fromkeys(sitemap_urls))[:3]
        visited: set[str] = set()
        records: list[SearchResultRecord] = []
        while queue and len(visited) < 4 and self._request_count < self.max_requests:
            sitemap_url = canonicalize_url(queue.pop(0))
            if (
                not sitemap_url
                or sitemap_url in visited
                or not url_belongs_to_domain(sitemap_url, domain)
            ):
                continue
            visited.add(sitemap_url)
            response = self._get(sitemap_url, deadline)
            if response is None:
                continue
            locations = [
                unescape(re.sub(r"\s+", "", item))
                for item in _SITEMAP_LOC_PATTERN.findall(response.text or "")
            ]
            locations.sort(key=lambda item: (
                0 if self._model_in_text(self._model, item) else 1,
                0 if any(
                    token in item.casefold() for token in ("product", "catalog", "shop")
                ) else 1,
                item,
            ))
            for location in locations:
                url = canonicalize_url(location)
                if not url or not url_belongs_to_domain(url, domain):
                    continue
                if url.lower().endswith((".xml", ".xml.gz")):
                    if len(queue) < 3:
                        queue.append(url)
                    continue
                if not self._model_in_text(self._model, url):
                    continue
                if _page_kind(url) in {"homepage", "catalog"}:
                    continue
                records.append(SearchResultRecord(
                    url=url,
                    title=unquote(urlparse(url).path).replace("-", " ").replace("_", " "),
                    snippet="Exact model found in manufacturer sitemap URL.",
                    provider=self.name,
                    raw_url=location,
                    parse_confidence="high",
                ))
        return records

    def _discover(self, brand: str, timeout_seconds: float) -> tuple[SearchResultRecord, ...]:
        deadline = time.monotonic() + max(0.1, timeout_seconds)
        domains = self._candidate_domains(brand)
        if not domains:
            return ()
        results: list[SearchResultRecord] = []
        for domain in domains:
            if time.monotonic() >= deadline or self._request_count >= self.max_requests:
                break
            url = f"https://www.{domain}/"
            response = self._get(url, deadline)
            if response is None:
                continue
            final_url = canonicalize_url(str(response.url))
            final_domain = _registrable_domain(_host(final_url))
            if not final_domain or _registrable_domain_label(final_domain) != _registrable_domain_label(domain):
                continue
            title = _extract_page_title(response.text)
            # Consistency check only, exactly as documented on
            # discover_global_official_domains: a brand-named title on a
            # mechanically exact brand-root domain is not itself proof of
            # official status, only a plausible candidate for the existing
            # corroboration logic to accept or reject. The literal word
            # "official" is never injected here -- that would fabricate the
            # one signal discover_global_official_domains treats as an
            # explicit claim.
            if normalize_model(brand) not in normalize_model(title or domain):
                continue
            results.append(SearchResultRecord(
                url=final_url,
                title=title or domain,
                snippet="Directly probed brand-root homepage.",
                provider=self.name,
                raw_url=url,
                redirect_url=url if final_url != canonicalize_url(url) else None,
                parse_confidence="high" if title else "medium",
            ))
            results.extend(self._matching_links(response.text, final_url, final_domain))

            robots = self._get(f"https://www.{domain}/robots.txt", deadline)
            declared: list[str] = []
            if robots is not None:
                declared = [
                    line.split(":", 1)[1].strip()
                    for line in (robots.text or "").splitlines()
                    if line.lower().startswith("sitemap:") and ":" in line
                ]
            results.extend(self._sitemap_records(
                [*declared, f"https://www.{domain}/sitemap.xml"],
                final_domain,
                deadline,
            ))

            if self._model and self._request_count < self.max_requests:
                search_url = f"https://www.{domain}/search?{urlencode({'q': self._model})}"
                search_response = self._get(search_url, deadline)
                if search_response is not None:
                    results.extend(self._matching_links(
                        search_response.text, str(search_response.url), final_domain,
                    ))

        unique: dict[str, SearchResultRecord] = {}
        for record in results:
            unique.setdefault(canonicalize_url(record.url), record)
        return tuple(record for url, record in unique.items() if url)

    def search_with_timeout(
        self,
        query: str,
        timeout_seconds: float,
    ) -> list[SearchResultLike]:
        self.last_raw_result_count = 0
        official_match = self._QUERY_PATTERN.match(query.strip())
        brand = self._brand or (official_match.group(1) if official_match else "")
        if not brand:
            return []
        is_official_query = bool(official_match)
        is_model_query = bool(
            self._model and self._model_in_text(self._model, query)
        )
        if not (is_official_query or is_model_query):
            return []
        if self._records is None:
            self._records = self._discover(brand, timeout_seconds)
        records = list(self._records)
        if is_official_query:
            # Preserve the provider's long-standing public tuple contract for
            # homepage bootstrap callers; the session normalizer enriches it.
            return [
                (record.raw_url or record.url, record.title)
                for record in records
                if _page_kind(record.url) == "homepage"
            ]
        return [
            record for record in records
            if self._model_in_text(self._model, f"{record.title} {record.url}")
        ]


def _provider_status(error: Exception) -> Literal["blocked", "timeout", "parse_error", "error"]:
    if isinstance(error, (ProviderTimeoutError, TimeoutError)):
        return "timeout"
    if isinstance(error, ProviderParseError):
        return "parse_error"
    lowered = (str(error) or error.__class__.__name__).lower()
    if any(marker in lowered for marker in ("timed out", "timeout", "deadline")):
        return "timeout"
    return "blocked" if any(marker in lowered for marker in (
        "bot-check", "blocked", "captcha", "recaptcha", "unusual traffic",
        "403", "429", "forbidden", "too many requests",
    )) else "error"


_CONNECTION_ERROR_MARKERS = (
    "connection reset", "connection aborted", "connection refused",
    "econnreset", "econnrefused", "name or service not known",
    "getaddrinfo failed", "network is unreachable", "remote end closed",
    "failed to establish a new connection", "nodename nor servname",
)


def _failure_class(status: str, error: Exception) -> str:
    """Map a provider status/exception onto a generic, retry-decision bucket.

    This is a distinct, finer-grained axis from ``ProviderStatus``: two
    providers can both report ``status="blocked"`` while one hit an actual
    WAF/bot challenge (not worth retrying) and the other hit an HTTP 429
    (worth a single bounded retry with backoff). Keeping ``status`` unchanged
    preserves every existing consumer of that field; ``failure_class`` is
    additive.
    """
    lowered = (str(error) or error.__class__.__name__).lower()
    if status == "timeout":
        return FailureClass.TIMEOUT
    if status == "parse_error":
        return FailureClass.MALFORMED
    if any(marker in lowered for marker in _CONNECTION_ERROR_MARKERS):
        return FailureClass.CONNECTION_ERROR
    if status == "blocked":
        if any(marker in lowered for marker in ("429", "too many requests", "rate limit")):
            return FailureClass.RATE_LIMITED
        return FailureClass.BLOCKED
    if status == "empty":
        return FailureClass.EMPTY
    return FailureClass.OTHER


_QUERY_QUALITY_STOPWORDS = {
    "and", "buy", "com", "details", "features", "for", "global", "manual",
    "official", "product", "review", "site", "spec", "specification",
    "specifications", "support", "the", "website", "with", "www",
}


def _query_result_quality(
    query: str,
    results: Iterable[SearchResultRecord],
) -> tuple[bool, int, int]:
    """Reject a non-empty SERP that carries no useful query identity signal.

    This is intentionally provider-neutral.  It uses only lexical overlap
    between the issued query and returned title/URL/snippet; it does not grant
    source authority or exact-model status.
    """
    without_sites = re.sub(r"\bsite:\S+", " ", query, flags=re.IGNORECASE)
    tokens = [
        token for token in re.findall(r"[\w]+", normalize_text(without_sites))
        if token not in _QUERY_QUALITY_STOPWORDS and (len(token) >= 2 or token.isdigit())
    ]
    tokens = list(dict.fromkeys(tokens))
    if not tokens:
        return True, 0, 0
    corpus = normalize_text(" ".join(
        f"{item.title} {item.url} {item.snippet}" for item in results
    ))
    corpus_tokens = set(re.findall(r"[\w]+", corpus))
    matched = sum(token in corpus_tokens for token in tokens)
    required = 1 if len(tokens) == 1 else max(2, (len(tokens) + 1) // 2)
    distinctive = [token for token in tokens if any(character.isdigit() for character in token)]
    distinctive_match = not distinctive or any(token in corpus_tokens for token in distinctive)
    return matched >= required and distinctive_match, matched, required


class ProviderSearchError(RuntimeError):
    """All configured providers failed for a single query."""

    def __init__(self, query: str, attempts: Iterable[ProviderAttempt]) -> None:
        self.query = query
        self.attempts = tuple(attempts)
        detail = self.attempts[-1].message if self.attempts else "No providers configured."
        super().__init__(detail)


def build_provider_query(provider: str, query: str, brand: str, model: str) -> str:
    """Adapt syntax to a provider while preserving the caller's intent.

    Bing and Google reliably treat a quoted model as one identity-bearing
    phrase. Naver's web endpoint has historically performed better with
    unquoted tokens. DDG and direct discovery retain the original query.
    This is product-agnostic and never adds a pre-known URL or domain.
    """
    query = " ".join((query or "").split())
    if provider == "naver":
        return query.replace('"', "")
    if provider not in {"bing", "google"} or not model:
        return query
    quoted_model = f'"{model}"'
    if quoted_model.casefold() in query.casefold():
        return query
    if any(
        normalize_model(model) in normalize_model(quoted)
        for quoted in re.findall(r'"([^"]+)"', query)
    ):
        return query
    match = re.search(re.escape(model), query, flags=re.IGNORECASE)
    if match:
        return f"{query[:match.start()]}{quoted_model}{query[match.end():]}"
    if "official website" in query.casefold() and brand:
        return f'{query} "{model}"'
    return query


class ResilientSearchSession:
    """Try fallback providers only after structured primary failure.

    A provider that keeps *succeeding* slowly is a distinct hazard from one
    that fails: the per-query fallback chain never advances past it, so a
    provider answering every query in 10-20s can, by itself, consume the
    entire shared workflow budget across a multi-query discovery plan before
    any alternate provider or the downstream fetch stage gets a turn. This
    session tracks each provider's cumulative elapsed time across the whole
    request (spanning initial and targeted discovery, which reuse the same
    session/budget) and stops offering it new queries once that cumulative
    time reaches its fair share of the total workflow budget
    (``config.provider_time_share``), regardless of whether its calls are
    still succeeding. This is generic capacity fairness, not a per-provider
    rule: it applies to whichever provider is first/slow in a given run.
    """

    def __init__(
        self,
        market: str = "global",
        providers: Iterable[SearchProvider] | None = None,
        *,
        config: DiscoveryRuntimeConfig | None = None,
        clock: Callable[[], float] = time.monotonic,
        budget: WallClockBudget | None = None,
        health_store: ProviderHealthStore | None = None,
    ) -> None:
        if market not in SUPPORTED_MARKETS:
            raise ValueError(f"Unsupported market: {market}")
        self.market = market
        self.config = config or DiscoveryRuntimeConfig()
        self._clock = clock
        self.budget = budget
        self.budget_stage = "discovery"
        # Opt-in cross-process health store: disabled by default (see
        # ProviderHealthStore.from_env), so existing callers/tests are
        # unaffected unless PDV_PROVIDER_HEALTH_PATH is explicitly set for
        # the run. This is layered on top of, not instead of, the
        # request-local circuit breaker below: the local one reacts within
        # this one product; the shared one lets a later product in the same
        # run skip straight past a provider several earlier products already
        # found dead, instead of rediscovering the same outage from scratch.
        self.health_store = health_store if health_store is not None else ProviderHealthStore.from_env()
        default_providers: tuple[SearchProvider, ...] = (
            DuckDuckGoHtmlSearchProvider(
                market, timeout_seconds=self.config.timeout_for("duckduckgo_html"),
            ),
            NaverSearchProvider(
                market, timeout_seconds=self.config.timeout_for("naver"),
            ),
            SeznamSearchProvider(
                market, timeout_seconds=self.config.timeout_for("seznam"),
            ),
            DuckDuckGoLiteSearchProvider(
                market, timeout_seconds=self.config.timeout_for("duckduckgo_lite"),
            ),
            BingSearchProvider(
                market, timeout_seconds=self.config.timeout_for("bing"),
            ),
            GoogleSearchSession(
                market, timeout_seconds=self.config.timeout_for("google"),
            ),
            # Last: an independent, SERP-free fallback that never shares a
            # failure domain with the five providers above (see class
            # docstring). Placed last so it never displaces a genuine SERP
            # "official" claim; it only ever runs when every provider above
            # it has already failed/emptied/circuit-opened for this query.
            DirectDomainProbeProvider(
                market, timeout_seconds=self.config.timeout_for("direct_domain_probe"),
            ),
        )
        disabled = {
            item.strip().casefold()
            for item in os.getenv("PDV_DISABLED_DISCOVERY_PROVIDERS", "").split(",")
            if item.strip()
        }
        self.providers = tuple(providers) if providers is not None else tuple(
            provider for provider in default_providers
            if provider.name.casefold() not in disabled
        )
        self._open_providers: dict[int, str] = {}
        self._failure_counts: dict[int, int] = {}
        self._provider_elapsed: dict[int, float] = {}
        self._brand = ""
        self._model = ""

    def configure_identity(self, brand: str, model: str) -> None:
        self._brand = " ".join((brand or "").split())
        self._model = " ".join((model or "").split())
        for provider in self.providers:
            configure = getattr(provider, "configure_identity", None)
            if configure is not None:
                configure(self._brand, self._model)

    def __enter__(self) -> "ResilientSearchSession":
        for provider in self.providers:
            enter = getattr(provider, "__enter__", None)
            if enter is not None:
                enter()
        return self

    def __exit__(self, *args: object) -> None:
        for provider in reversed(self.providers):
            exit_provider = getattr(provider, "__exit__", None)
            if exit_provider is not None:
                exit_provider(*args)

    def release_transient_resources(self) -> None:
        """Release provider browsers between discovery and document fetching."""
        errors: list[Exception] = []
        for provider in reversed(self.providers):
            release = getattr(provider, "release_transient_resources", None)
            if release is None:
                continue
            try:
                release()
            except Exception as error:
                errors.append(error)
        if errors:
            raise RuntimeError(
                f"Search provider cleanup failed: {errors[0]}"
            ) from errors[0]

    def search_with_status(self, query: str) -> ProviderQueryOutcome:
        attempts: list[ProviderAttempt] = []
        collected: list[SearchResultLike] = []
        productive_provider_found = False
        for index, provider in enumerate(self.providers):
            # Once a productive SERP path has answered, avoid multiplying
            # third-party requests. Supplemental independent paths still run
            # and are merged deterministically in configured provider order.
            if productive_provider_found and not getattr(provider, "always_run", False):
                continue
            effective_query = build_provider_query(
                provider.name, query, self._brand, self._model,
            )
            budget_before = (
                round(self.budget.remaining_seconds, 6)
                if self.budget is not None else None
            )
            timeout_seconds = self.config.timeout_for(provider.name)
            if self.budget is not None:
                bounded_timeout = self.budget.timeout_for(
                    timeout_seconds,
                    self.budget_stage,
                )
                if bounded_timeout is None:
                    attempts.append(ProviderAttempt(
                        provider=provider.name,
                        query=query,
                        status="timeout",
                        message=self.budget.exhaustion_reason,
                        is_fallback=index > 0,
                        timeout_seconds=0.0,
                        timed_out=True,
                        budget_exhausted=True,
                        effective_query=effective_query,
                        budget_before_seconds=budget_before,
                        budget_after_seconds=0.0,
                    ))
                    break
                timeout_seconds = bounded_timeout
            if index in self._open_providers:
                attempts.append(ProviderAttempt(
                    provider=provider.name,
                    query=query,
                    status="circuit_open",
                    message=self._open_providers[index],
                    is_fallback=index > 0,
                    timeout_seconds=timeout_seconds,
                    circuit_open=True,
                    effective_query=effective_query,
                    budget_before_seconds=budget_before,
                    budget_after_seconds=budget_before,
                ))
                continue
            health_scope = provider.name
            if provider.name == "direct_domain_probe" and self._brand:
                # Direct probes touch unrelated manufacturer hosts. A timeout
                # at one brand is not evidence that another brand's domain is
                # unhealthy, so cross-process health must follow that actual
                # failure domain instead of globally disabling the provider.
                brand_scope = re.sub(r"[^a-z0-9]", "", self._brand.casefold())
                health_scope = f"{provider.name}:{brand_scope}"
            shared_key = f"discovery:{health_scope}"
            if self.health_store.is_open(shared_key):
                message = (
                    f"Shared circuit open for {provider.name}: repeated failures "
                    "observed elsewhere in this run; skipping without a request."
                )
                attempts.append(ProviderAttempt(
                    provider=provider.name,
                    query=query,
                    status="circuit_open",
                    message=message,
                    is_fallback=index > 0,
                    timeout_seconds=timeout_seconds,
                    circuit_open=True,
                    shared_circuit_open=True,
                    effective_query=effective_query,
                    budget_before_seconds=budget_before,
                    budget_after_seconds=budget_before,
                ))
                self._open_providers[index] = message
                continue
            if self.budget is not None:
                cap = self.budget.total_seconds * self.config.provider_time_share
                spent = self._provider_elapsed.get(index, 0.0)
                if spent >= cap:
                    attempts.append(ProviderAttempt(
                        provider=provider.name,
                        query=query,
                        status="capped",
                        message=(
                            f"{provider.name} has used {spent:.1f}s, its "
                            f"{cap:.1f}s fair share of the workflow budget; "
                            "yielding remaining queries to other providers."
                        ),
                        is_fallback=index > 0,
                        timeout_seconds=timeout_seconds,
                        provider_time_capped=True,
                        effective_query=effective_query,
                        budget_before_seconds=budget_before,
                        budget_after_seconds=budget_before,
                    ))
                    continue
            started = self._clock()
            retried = False
            for attempt_number in range(2):
                try:
                    bounded_search = getattr(provider, "search_with_timeout", None)
                    if bounded_search is not None:
                        raw_results = tuple(bounded_search(effective_query, timeout_seconds))
                    else:
                        raw_results = tuple(provider.search(effective_query))
                    break
                except Exception as error:
                    status = _provider_status(error)
                    failure_class = _failure_class(status, error)
                    # A single bounded retry, only for a fast-fail connection
                    # error (DNS/refused/reset) and only when there is still
                    # budget headroom -- retrying a WAF block or a timeout
                    # that already consumed its full deadline would just
                    # repeat the same outcome at double the cost, monopolizing
                    # the shared workflow budget (see Stage 24 finding).
                    can_retry = (
                        attempt_number == 0
                        and failure_class == FailureClass.CONNECTION_ERROR
                        and (self.budget is None or self.budget.remaining_seconds > 1.0)
                    )
                    if can_retry:
                        retried = True
                        time.sleep(RETRY_BACKOFF_SECONDS)
                        continue
                    duration = max(0.0, self._clock() - started)
                    self._provider_elapsed[index] = (
                        self._provider_elapsed.get(index, 0.0) + duration
                    )
                    attempts.append(ProviderAttempt(
                        provider=provider.name,
                        query=query,
                        status=status,
                        message=str(error) or error.__class__.__name__,
                        is_fallback=index > 0,
                        duration_seconds=round(duration, 6),
                        timeout_seconds=timeout_seconds,
                        timed_out=status == "timeout",
                        blocked=status == "blocked",
                        parse_failure=status == "parse_error",
                        exception_class=type(error).__name__,
                        transport=getattr(provider, "last_transport", None),
                        failure_class=failure_class,
                        retried=retried,
                        effective_query=effective_query,
                        budget_before_seconds=budget_before,
                        budget_after_seconds=(
                            round(self.budget.remaining_seconds, 6)
                            if self.budget is not None else None
                        ),
                    ))
                    self._failure_counts[index] = self._failure_counts.get(index, 0) + 1
                    if failure_class in HEALTH_AFFECTING_FAILURE_CLASSES:
                        self.health_store.record_failure(shared_key, failure_class)
                    if status in {"blocked", "timeout", "parse_error"} or (
                        self._failure_counts[index] >= self.config.circuit_breaker_failures
                    ):
                        self._open_providers[index] = (
                            f"Circuit open after {status}: {str(error) or type(error).__name__}"
                        )
                    raw_results = None
                    break
            if raw_results is None:
                continue
            duration = max(0.0, self._clock() - started)
            self._provider_elapsed[index] = self._provider_elapsed.get(index, 0.0) + duration
            results = _normalized_search_results(raw_results, provider.name, effective_query)
            raw_result_count = int(
                getattr(provider, "last_raw_result_count", len(raw_results))
            )
            parsed_result_count = len(results)
            deduped_result_count = len({
                url
                for item in results
                if (url := canonicalize_url(item.url))
            })
            if duration > timeout_seconds:
                error = ProviderTimeoutError(
                    f"{provider.name} exceeded its {timeout_seconds:g}s deadline."
                )
                attempts.append(ProviderAttempt(
                    provider=provider.name,
                    query=query,
                    status="timeout",
                    message=str(error),
                    is_fallback=index > 0,
                    duration_seconds=round(duration, 6),
                    timeout_seconds=timeout_seconds,
                    timed_out=True,
                    exception_class=type(error).__name__,
                    raw_result_count=raw_result_count,
                    parsed_result_count=parsed_result_count,
                    deduped_result_count=deduped_result_count,
                    transport=getattr(provider, "last_transport", None),
                    failure_class=FailureClass.TIMEOUT,
                    effective_query=effective_query,
                    budget_before_seconds=budget_before,
                    budget_after_seconds=(
                        round(self.budget.remaining_seconds, 6)
                        if self.budget is not None else None
                    ),
                ))
                self._open_providers[index] = f"Circuit open after timeout: {error}"
                self.health_store.record_failure(shared_key, FailureClass.TIMEOUT)
                continue
            if not results:
                attempts.append(ProviderAttempt(
                    provider=provider.name,
                    query=query,
                    status="empty",
                    result_count=0,
                    message="Provider returned no candidates.",
                    is_fallback=index > 0,
                    duration_seconds=round(duration, 6),
                    timeout_seconds=timeout_seconds,
                    raw_result_count=raw_result_count,
                    parsed_result_count=parsed_result_count,
                    deduped_result_count=deduped_result_count,
                    transport=getattr(provider, "last_transport", None),
                    effective_query=effective_query,
                    budget_before_seconds=budget_before,
                    budget_after_seconds=(
                        round(self.budget.remaining_seconds, 6)
                        if self.budget is not None else None
                    ),
                ))
                continue
            useful, matched_tokens, required_tokens = (
                _query_result_quality(effective_query, results)
                if getattr(provider, "quality_gate", False)
                else (True, 0, 0)
            )
            if not useful:
                attempts.append(ProviderAttempt(
                    provider=provider.name,
                    query=query,
                    status="low_value",
                    result_count=len(results),
                    message=(
                        "Provider results lacked query identity signal "
                        f"({matched_tokens}/{required_tokens} required tokens matched)."
                    ),
                    is_fallback=index > 0,
                    duration_seconds=round(duration, 6),
                    timeout_seconds=timeout_seconds,
                    raw_result_count=raw_result_count,
                    parsed_result_count=parsed_result_count,
                    deduped_result_count=deduped_result_count,
                    transport=getattr(provider, "last_transport", None),
                    effective_query=effective_query,
                    budget_before_seconds=budget_before,
                    budget_after_seconds=(
                        round(self.budget.remaining_seconds, 6)
                        if self.budget is not None else None
                    ),
                ))
                continue
            self._failure_counts[index] = 0
            self.health_store.record_success(shared_key)
            exact_model_hit = bool(
                self._model and any(
                    model_match(
                        self._model,
                        f"{item.title} {item.snippet} {item.url}",
                    ) == "exact"
                    for item in results
                )
            )
            official_domain_hit = bool(
                "official" in effective_query.casefold()
                and any(
                    normalize_model(self._brand) in normalize_model(item.title)
                    for item in results
                )
            )
            ddg_unavailable = not any(
                item.provider == "duckduckgo_html" and item.status == "success"
                for item in attempts
            )
            attempts.append(ProviderAttempt(
                provider=provider.name,
                query=query,
                status="success",
                result_count=len(results),
                is_fallback=index > 0,
                duration_seconds=round(duration, 6),
                timeout_seconds=timeout_seconds,
                raw_result_count=raw_result_count,
                parsed_result_count=parsed_result_count,
                deduped_result_count=deduped_result_count,
                transport=getattr(provider, "last_transport", None),
                effective_query=effective_query,
                exact_model_hit=exact_model_hit,
                official_domain_hit=official_domain_hit,
                budget_before_seconds=budget_before,
                budget_after_seconds=(
                    round(self.budget.remaining_seconds, 6)
                    if self.budget is not None else None
                ),
                independent_success_without_ddg=(
                    provider.name != "duckduckgo_html" and ddg_unavailable
                ),
            ))
            collected.extend(results)
            if not getattr(provider, "always_run", False):
                productive_provider_found = True
        return ProviderQueryOutcome(tuple(collected), tuple(attempts))

    def search(self, query: str) -> list[SearchResultLike]:
        outcome = self.search_with_status(query)
        if outcome.attempts and all(item.status != "success" for item in outcome.attempts):
            raise ProviderSearchError(query, outcome.attempts)
        return list(outcome.results)


def google_search(query: str, market: str = "global") -> list[SearchResultLike]:
    """Search once, closing a fallback browser afterwards if one was needed."""
    with GoogleSearchSession(market) as session:
        return session.search(query)


def _discovery_issue(query: str, error: Exception) -> DiscoveryIssue:
    message = str(error) or error.__class__.__name__
    return DiscoveryIssue(_provider_status(error), query, message)


def _searcher_name(searcher: object) -> str:
    owner = getattr(searcher, "__self__", None)
    return str(
        getattr(owner, "name", None)
        or getattr(searcher, "name", None)
        or getattr(searcher, "__name__", None)
        or "injected"
    )


def _configure_searcher_identity(
    searcher: object,
    brand: str,
    model: str,
) -> None:
    owner = getattr(searcher, "__self__", None)
    configure = getattr(owner or searcher, "configure_identity", None)
    if configure is not None:
        configure(brand, model)


def _search_query(
    searcher: Callable[[str], Iterable[SearchResultLike] | ProviderQueryOutcome],
    query: str,
) -> tuple[list[SearchResultLike], list[ProviderAttempt], list[DiscoveryIssue]]:
    try:
        response = searcher(query)
    except ProviderSearchError as error:
        attempts = list(error.attempts)
        issues = [
            DiscoveryIssue(item.status, query, item.message or "Search failed.", item.provider)
            for item in attempts
            if item.status != "success"
        ]
        return [], attempts, issues
    except Exception as error:
        issue = _discovery_issue(query, error)
        provider = _searcher_name(searcher)
        issue = DiscoveryIssue(issue.status, issue.query, issue.message, provider)
        attempt = ProviderAttempt(provider, query, issue.status, message=issue.message)
        return [], [attempt], [issue]

    if isinstance(response, ProviderQueryOutcome):
        attempts = list(response.attempts)
        issues = [
            DiscoveryIssue(item.status, query, item.message or "Search failed.", item.provider)
            for item in attempts
            if item.status != "success"
        ]
        provider = next(
            (item.provider for item in reversed(attempts) if item.status == "success"),
            _searcher_name(searcher),
        )
        return list(_normalized_search_results(response.results, provider, query)), attempts, issues

    raw_results = list(response)
    provider = _searcher_name(searcher)
    if not raw_results:
        attempt = ProviderAttempt(
            provider, query, "empty", message="Provider returned no candidates.",
        )
        return [], [attempt], [
            DiscoveryIssue("empty", query, attempt.message or "Empty result.", provider),
        ]
    results = list(_normalized_search_results(raw_results, provider, query))
    return results, [ProviderAttempt(provider, query, "success", len(results))], []


def _search_status_from_issues(issues: Iterable[DiscoveryIssue]) -> SearchStatus:
    statuses = [item.status for item in issues]
    if not statuses:
        return "success"
    for status in (
        "timeout", "blocked", "error", "parse_error", "low_value", "empty",
        "circuit_open", "capped",
    ):
        if status not in statuses:
            continue
        if status in {"parse_error", "low_value", "circuit_open", "capped"}:
            return "error"
        return status  # type: ignore[return-value]
    return "error"


def _annotate_attempt_candidate_counts(
    attempts: Iterable[ProviderAttempt],
    candidates: Iterable[Candidate],
    rejected_candidates: Iterable[Candidate],
) -> list[ProviderAttempt]:
    """Attach post-ranking conversion counts to provider telemetry."""
    accepted: dict[str, set[str]] = {}
    rejected: dict[str, set[str]] = {}
    for bucket, items in ((accepted, candidates), (rejected, rejected_candidates)):
        for candidate in items:
            providers = {
                str(item.get("provider") or "")
                for item in candidate.get("discovery_provenance") or ()
            } or {str(candidate.get("discovery_provider") or "")}
            for provider in providers:
                if provider:
                    bucket.setdefault(provider, set()).add(candidate["url"])
    return [
        replace(
            attempt,
            accepted_candidate_count=len(accepted.get(attempt.provider, ())),
            rejected_candidate_count=len(rejected.get(attempt.provider, ())),
        )
        for attempt in attempts
    ]


def _official_query_returned_results(attempts: Iterable[ProviderAttempt]) -> bool:
    """Any provider - not only the first/primary one - may seed official-domain
    evidence for an "official website" query.

    Stage 18.7: requiring specifically the *non-fallback* provider to be the
    one that succeeded made the entire request's authority corroboration
    depend on one search provider (previously Google) being available for
    this one query, even though ``discover_global_official_domains`` already
    applies an evidence-based filter (domain/brand consistency, an explicit
    "official" claim, and the brand name in the result title) regardless of
    which provider returned the result. That filter - not provider identity -
    is what makes a claim trustworthy, so any provider's success is eligible
    on equal terms.
    """
    return any(item.status == "success" for item in attempts)


def discover_with_status(
    brand: str,
    model: str,
    article: str | None = None,
    market: str = "global",
    searcher: Searcher | None = None,
) -> DiscoveryOutcome:
    """Discover sources and retain blocked/error state as structured data."""
    brand = " ".join((brand or "").split())
    model = " ".join((model or "").split())
    article = " ".join(article.split()) if article and article.strip() else None
    if not brand or not model:
        raise ValueError("brand and model are required")
    if market not in SUPPORTED_MARKETS:
        raise ValueError(f"Unsupported market: {market}")

    def run(
        active_searcher: Callable[[str], Iterable[SearchResultLike] | ProviderQueryOutcome],
    ) -> DiscoveryOutcome:
        _configure_searcher_identity(active_searcher, brand, model)
        cache_key = (normalize_model(brand), market)
        cached = _OFFICIAL_DOMAIN_CACHE.get(cache_key)
        official_domains = dict(cached or ())
        raw_results: list[SearchResultLike] = []
        official_results: list[SearchResultLike] = []
        issues: list[DiscoveryIssue] = []
        provider_attempts: list[ProviderAttempt] = []
        attempted_queries: list[str] = []
        base_queries = build_search_queries(brand, model, article)
        # Identity-bearing queries run before the broad authority bootstrap.
        # A bot-check on a low-specificity "official website" SERP must not
        # open a provider circuit before it has a chance to return product
        # pages for the exact model.
        brand_domain_hint = re.sub(r"[^a-z0-9]", "", brand.casefold())
        hinted_site_query = (
            f'"{model}" site:{brand_domain_hint}.com'
            if len(brand_domain_hint) >= 3 else None
        )
        queries: list[str] = list(base_queries)
        if hinted_site_query:
            queries.append(hinted_site_query)
        if cached is None:
            queries.extend((f"{brand} official website", f"{brand} official {model}"))
        budget_stopped = False
        for query in queries:
            attempted_queries.append(query)
            found, attempts, query_issues = _search_query(active_searcher, query)
            provider_attempts.extend(attempts)
            issues.extend(query_issues)
            raw_results.extend(found)
            if "official" in query and _official_query_returned_results(attempts):
                official_results.extend(found)
            if any(item.budget_exhausted for item in attempts):
                budget_stopped = True
                break
        if cached is None:
            official_domains = dict(discover_global_official_domains(
                brand,
                official_results,
                product_results=raw_results,
                model=model,
            ))
            _OFFICIAL_DOMAIN_CACHE[cache_key] = tuple(official_domains.items())
        relevant_domains = [
            domain for domain in official_domains
            if any(
                url_belongs_to_domain((record := _search_result_record(item)).url, domain)
                and model_match(
                    model,
                    f"{record.title} {record.snippet} {record.url}",
                ) == "exact"
                for item in raw_results
            )
        ]
        site_domains = relevant_domains or list(official_domains)[:1]
        for domain in (() if budget_stopped else site_domains[:3]):
            query = f'"{model}" site:{domain}'
            if query in queries:
                continue
            queries.append(query)
            attempted_queries.append(query)
            found, attempts, query_issues = _search_query(active_searcher, query)
            provider_attempts.extend(attempts)
            issues.extend(query_issues)
            raw_results.extend(found)
            if any(item.budget_exhausted for item in attempts):
                break
        ranked_candidates = rank_candidates(
            raw_results, brand, model, article, market=market,
            official_domains=official_domains,
        )
        candidates, rejected_candidates = _partition_relevance_candidates(
            ranked_candidates, brand, model, article,
        )
        provider_attempts = _annotate_attempt_candidate_counts(
            provider_attempts, candidates, rejected_candidates,
        )
        if issues and raw_results:
            status: SearchStatus = "partial"
        elif issues:
            status = _search_status_from_issues(issues)
        else:
            status = "success"
        return DiscoveryOutcome(
            candidates=candidates,
            search_status=status,
            queries=queries,
            attempted_queries=attempted_queries,
            issues=issues,
            provider_attempts=provider_attempts,
            rejected_candidates=rejected_candidates,
        )

    if searcher is not None:
        return run(searcher)
    with ResilientSearchSession(market) as session:
        return run(session.search_with_status)


def discover(brand: str, model: str, article: str | None = None,
             market: str = "global", searcher: Searcher | None = None) -> list[Candidate]:
    """Compatibility list API; failed empty searches raise with an outcome."""
    outcome = discover_with_status(brand, model, article, market, searcher)
    if outcome.search_status != "success" and not outcome.candidates:
        raise DiscoverySearchError(outcome)
    return outcome.candidates


def _source_identity_relation(identity: ProductIdentity, candidate: Candidate) -> str:
    match = candidate["model_match"]
    if match in {"mismatch", "different_variant"}:
        return "different_model"
    if match not in {"exact", "likely_variant", "likely"}:
        if not base_model_in_text(
            identity.base_model,
            f"{candidate['title']} {urlparse(candidate['url']).path}",
        ):
            return "unknown"
    if identity.variant_suffix:
        full_model = f"{identity.base_model}/{identity.variant_suffix}"
        if model_match(full_model, f"{candidate['title']} {candidate['url']}") == "exact":
            return "exact_variant"
    return "same_base_model"


def _source_verification_evidence(
    identity: ProductIdentity,
    candidate: Candidate,
) -> list[str]:
    haystack = f"{candidate['title']} {candidate['url']}"
    normalized_haystack = normalize_text(haystack)
    found: list[str] = []
    signals = identity_verification_signals(identity)
    for name, value in signals.items():
        if name in {"ram", "storage"}:
            continue
        if name == "color":
            if normalize_text(value) in normalized_haystack.split():
                found.append(f"color={value}")
        elif article_matches(value, haystack):
            found.append(f"{name}={value}")
    ram = identity.configuration.get("ram")
    storage = identity.configuration.get("storage")
    if ram and storage:
        ram_number = re.sub(r"\D", "", ram)
        storage_number = re.sub(r"\D", "", storage)
        if re.search(
            rf"(?<!\d){re.escape(ram_number)}\s*(?:GB\s*)?(?:\+|/)\s*"
            rf"{re.escape(storage_number)}\s*(?:GB)?(?!\d)",
            haystack,
            re.IGNORECASE,
        ):
            found.append(f"configuration={ram}+{storage}")
    return found


def _annotate_identity_candidates(
    outcome: DiscoveryOutcome,
    identity: ProductIdentity,
    model: str,
    article: str | None,
) -> DiscoveryOutcome:
    """Apply the same identity annotations to broad and targeted discovery."""
    for candidate in outcome.candidates:
        if (
            candidate["model_match"] in {"unknown", "mismatch"}
            and base_model_in_text(identity.base_model, candidate["title"])
        ):
            candidate["model_match"] = "exact"
            candidate["product_match_evidence"] = (
                "Equivalent complete base-model phrase found in title/snippet."
            )
            candidate["score"] = score_candidate(
                candidate["url"], candidate["title"], candidate["source_type"],
                candidate["authority_status"], "exact", model, article, identity.brand,
            )
            candidate["model_relevance"] = "exact_base_model"
        candidate["identity_relation"] = _source_identity_relation(identity, candidate)
        candidate["identity_verification_evidence"] = _source_verification_evidence(
            identity, candidate,
        )
    outcome.candidates.sort(key=lambda item: (-item["score"], item["url"]))
    return outcome


def discover_identity_query_with_status(
    identity: ProductIdentity,
    query: str,
    market: str = "global",
    searcher: Searcher | None = None,
) -> DiscoveryOutcome:
    """Run one explicit field query through Stage 1 ranking and identity checks.

    Official-domain discovery remains process-cached, so a targeted run can issue
    several field queries without repeatedly performing the authority lookup.
    """
    model = identity.base_model or identity.commercial_model
    query = " ".join((query or "").split())
    if not model:
        raise ValueError("identity must contain a base_model or commercial_model")
    if not query:
        raise ValueError("query is required")
    if market not in SUPPORTED_MARKETS:
        raise ValueError(f"Unsupported market: {market}")
    article = identity.manufacturer_article or identity.product_code or identity.sku
    if not article and identity.candidate_identifiers:
        article = identity.candidate_identifiers[0]

    def run(
        active_searcher: Callable[[str], Iterable[SearchResultLike] | ProviderQueryOutcome],
    ) -> DiscoveryOutcome:
        _configure_searcher_identity(active_searcher, identity.brand, model)
        cache_key = (normalize_model(identity.brand), market)
        cached = _OFFICIAL_DOMAIN_CACHE.get(cache_key)
        official_domains = dict(cached or ())
        issues: list[DiscoveryIssue] = []
        provider_attempts: list[ProviderAttempt] = []
        attempted: list[str] = []
        raw_results: list[SearchResultLike] = []
        authority_results: Iterable[SearchResultLike] = ()

        if cached is None:
            authority_query = f"{identity.brand} official website"
            attempted.append(authority_query)
            official_results, attempts, query_issues = _search_query(
                active_searcher, authority_query,
            )
            provider_attempts.extend(attempts)
            issues.extend(query_issues)
            authority_results = (
                official_results if _official_query_returned_results(attempts) else ()
            )
            official_domains = dict(
                discover_global_official_domains(identity.brand, authority_results)
            )
            if any(item.budget_exhausted for item in attempts):
                _OFFICIAL_DOMAIN_CACHE[cache_key] = tuple(official_domains.items())
                ranked_candidates = rank_candidates(
                    raw_results,
                    identity.brand,
                    model,
                    article,
                    market=market,
                    official_domains=official_domains,
                )
                candidates, rejected_candidates = _partition_relevance_candidates(
                    ranked_candidates, identity.brand, model, article,
                )
                return _annotate_identity_candidates(
                    DiscoveryOutcome(
                        candidates,
                        _search_status_from_issues(issues),
                        [query],
                        attempted,
                        issues,
                        provider_attempts,
                        rejected_candidates,
                    ),
                    identity,
                    model,
                    article,
                )

        attempted.append(query)
        found, attempts, query_issues = _search_query(active_searcher, query)
        provider_attempts.extend(attempts)
        issues.extend(query_issues)
        raw_results.extend(found)

        if cached is None:
            official_domains = dict(discover_global_official_domains(
                identity.brand,
                authority_results,
                product_results=raw_results,
                model=model,
            ))
            _OFFICIAL_DOMAIN_CACHE[cache_key] = tuple(official_domains.items())

        ranked_candidates = rank_candidates(
            raw_results,
            identity.brand,
            model,
            article,
            market=market,
            official_domains=official_domains,
        )
        candidates, rejected_candidates = _partition_relevance_candidates(
            ranked_candidates, identity.brand, model, article,
        )
        provider_attempts = _annotate_attempt_candidate_counts(
            provider_attempts, candidates, rejected_candidates,
        )
        if issues and raw_results:
            status: SearchStatus = "partial"
        elif issues:
            status = _search_status_from_issues(issues)
        else:
            status = "success"
        return _annotate_identity_candidates(
            DiscoveryOutcome(
                candidates, status, [query], attempted, issues, provider_attempts,
                rejected_candidates,
            ),
            identity,
            model,
            article,
        )

    if searcher is not None:
        return run(searcher)
    with ResilientSearchSession(market) as session:
        return run(session.search_with_status)


def discover_identity_with_status(
    identity: ProductIdentity,
    market: str = "global",
    searcher: Searcher | None = None,
) -> DiscoveryOutcome:
    """Discover by base model and retain variant fields as verification signals."""
    model = identity.base_model or identity.commercial_model
    if not model:
        raise ValueError("identity must contain a base_model or commercial_model")
    article = identity.manufacturer_article or identity.product_code or identity.sku
    if not article and identity.candidate_identifiers:
        article = identity.candidate_identifiers[0]
    outcome = discover_with_status(identity.brand, model, article, market, searcher)
    return _annotate_identity_candidates(outcome, identity, model, article)


def discover_identity(
    identity: ProductIdentity,
    market: str = "global",
    searcher: Searcher | None = None,
) -> list[Candidate]:
    """Compatibility list view of identity-aware structured discovery."""
    outcome = discover_identity_with_status(identity, market, searcher)
    if outcome.search_status != "success" and not outcome.candidates:
        raise DiscoverySearchError(outcome)
    return outcome.candidates
