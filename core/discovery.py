"""Generic first-stage discovery of plausible product source pages."""

from __future__ import annotations

from dataclasses import dataclass, field
from html.parser import HTMLParser
import os
from pathlib import Path
import re
import tempfile
import time
from typing import Callable, Iterable, Literal, Protocol, TypedDict
from urllib.parse import parse_qsl, parse_qs, quote_plus, unquote, urlencode, urlparse, urlunparse
from urllib.request import Request, urlopen

from core.match import article_matches, candidate_model_match, model_match, normalize_model, normalize_text
from core.identity import ProductIdentity, base_model_in_text, identity_verification_signals


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


SearchResult = tuple[str, str]
SearchStatus = Literal["success", "partial", "blocked", "error"]
ProviderStatus = Literal["success", "blocked", "error"]
RelevanceRelation = Literal["exact", "likely_variant", "weak", "reject"]


class SearchProvider(Protocol):
    """Interchangeable transport for one web-search provider."""

    name: str

    def search(self, query: str) -> Iterable[SearchResult]: ...


@dataclass(frozen=True, slots=True)
class ProviderAttempt:
    """Search-provider reliability metadata, independent of source authority."""

    provider: str
    query: str
    status: ProviderStatus
    result_count: int = 0
    message: str | None = None
    is_fallback: bool = False


@dataclass(frozen=True, slots=True)
class ProviderQueryOutcome:
    results: tuple[SearchResult, ...] = ()
    attempts: tuple[ProviderAttempt, ...] = ()


Searcher = Callable[[str], Iterable[SearchResult] | ProviderQueryOutcome]


@dataclass(frozen=True, slots=True)
class DiscoveryIssue:
    status: Literal["blocked", "error"]
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

MARKETPLACE_DOMAINS = {
    "amazon", "aliexpress", "ebay", "ozon", "temu", "wildberries",
}
BLOCKED_DOMAINS = {
    "facebook.com", "google.com", "instagram.com", "linkedin.com",
    "pinterest.com", "tiktok.com", "twitter.com", "vk.com", "x.com",
    "youtube.com", "youtu.be",
}
BLOCKED_PATH_SEGMENTS = {
    "about", "account", "blog", "contact", "login", "news", "privacy",
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
    "spare", "wallet",
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


def is_obvious_non_product_url(url: str) -> bool:
    if not url or not _host(url):
        return True
    host = _host(url)
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


def discover_global_official_domains(brand: str, results: Iterable[SearchResult]) -> list[tuple[str, str]]:
    """Return conservatively proven official domains and their evidence URLs."""
    brand_key = normalize_model(brand).lower()
    ranked: list[tuple[int, str, str]] = []
    for position, (url, title) in enumerate(results):
        domain = _host(url)
        label = re.sub(r"[^a-z0-9]", "", domain.split(".")[0])
        title_norm = normalize_text(title)
        if not brand_key or (brand_key not in label and label not in brand_key):
            continue
        brand_in_title = normalize_model(brand) in normalize_model(title)
        official_signal = "official" in title_norm or "официаль" in title_norm
        # Domain/brand similarity is only a consistency check. Verification
        # requires an explicit result claim linking this brand to an official
        # site; a similarly named homepage is not evidence by itself.
        if not official_signal or not brand_in_title:
            continue
        ranked.append((120 - position, domain, canonicalize_url(url)))
    ranked.sort(reverse=True)
    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    for _, domain, evidence_url in ranked:
        if domain not in seen:
            seen.add(domain)
            found.append((domain, evidence_url))
    return found


def discover_global_official_domain(brand: str, results: Iterable[SearchResult]) -> str | None:
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
    return path.endswith(".pdf") or _path_has_hint(segments, SUPPORT_PATH_HINTS)


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
    if official_domain and (domain == official_domain or domain.endswith(f".{official_domain}")):
        return "manufacturer"
    if _is_marketplace_domain(domain):
        return "marketplace"
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
    tokens = set(normalize_text(f"{title} {urlparse(url).path}").split())
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
    url = str(candidate.get("url") or "")
    haystack = f"{title} {url}"
    match = str(candidate.get("model_match") or "unknown")
    exact_phrase = base_model_in_text(model, haystack)
    exact_article = article_matches(article, haystack)
    component_relation = _model_component_relation(model, haystack)

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

    brand_present = normalize_model(brand) in normalize_model(haystack)
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


def rank_candidates(results: Iterable[SearchResult], brand: str, model: str,
                    article: str | None = None, official_domain: str | None = None,
                    authority_evidence_url: str | None = None,
                    market: str = "global",
                    official_domains: dict[str, str] | None = None) -> list[Candidate]:
    candidates: dict[str, Candidate] = {}
    for raw_url, title in results:
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
            authority_reason = "Domain verified from an explicit brand official-site result."
            if _is_official_document(url):
                source_type = "official_document"
        match = candidate_model_match(model, title, urlparse(url).path)
        product_match_evidence = None
        if match == "exact":
            product_match_evidence = "Exact normalized model token found in title/snippet or URL."
        elif match == "likely_variant":
            product_match_evidence = "Base model found with an explicit variant suffix."
        elif match == "likely":
            product_match_evidence = "Model-like identifier extends the requested base model."
        elif article_matches(article, f"{title} {url}"):
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
                url, title, source_type, authority_status, match, model, article, brand,
            ),
            "identity_relation": "unknown",
            "identity_verification_evidence": [],
            "relevance_relation": "reject",
            "relevance_reasons": [],
        }
        previous = candidates.get(url)
        if previous is None or candidate["score"] > previous["score"]:
            candidates[url] = candidate
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
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.path == "/url" and (host == "google.com" or host.endswith(".google.com")):
        query = parse_qs(parsed.query)
        url = (query.get("q") or query.get("url") or [""])[0]
    elif url.startswith("/url?"):
        url = (parse_qs(urlparse(url).query).get("q") or [""])[0]
    return canonicalize_url(unquote(url))


def needs_playwright_fallback(results: Iterable[SearchResult], html: str) -> bool:
    """Decide whether the HTTP response contains usable organic results."""
    text = (html or "").lower()
    external = [url for url, _ in results if _is_external_result(url)]
    return not external or any(marker in text for marker in INTERSTITIAL_MARKERS)


def _http_google_search(query: str) -> tuple[list[SearchResult], str]:
    return _http_google_search_for_market(query, "global")


def _google_search_url(query: str, market: str) -> str:
    if market not in SUPPORTED_MARKETS:
        raise ValueError(f"Unsupported market: {market}")
    params = {"q": query, "num": "10", **MARKET_GOOGLE_PARAMS[market]}
    return f"https://www.google.com/search?{urlencode(params)}"


def _http_google_search_for_market(query: str, market: str) -> tuple[list[SearchResult], str]:
    request = Request(
        _google_search_url(query, market),
        headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "en-US,en;q=0.8"},
    )
    try:
        with urlopen(request, timeout=15) as response:
            html = response.read().decode("utf-8", errors="replace")
    except Exception:
        return [], ""
    parser = _GoogleParser()
    parser.feed(html)
    return [(url, title) for url, title in parser.results if _is_external_result(url)], html


class GoogleSearchSession:
    """HTTP-first Google search with one lazy Playwright browser per discovery."""

    name = "google"

    def __init__(self, market: str = "global") -> None:
        if market not in SUPPORTED_MARKETS:
            raise ValueError(f"Unsupported market: {market}")
        self.market = market
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None

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

    def search(self, query: str) -> list[SearchResult]:
        results, html = _http_google_search_for_market(query, self.market)
        if not needs_playwright_fallback(results, html):
            return results
        return self._playwright_search(query)

    def _ensure_page(self):
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
            )
        except Exception as error:
            self._playwright.stop()
            self._playwright = None
            raise RuntimeError(
                "Playwright Chromium is unavailable. Run 'playwright install chromium'."
            ) from error
        self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
        return self._page

    @staticmethod
    def _accept_consent(page) -> bool:
        for label in ("Accept all", "I agree", "Accept", "Принять все", "Согласен"):
            try:
                button = page.get_by_text(label, exact=True)
                if button.count():
                    button.first.click(timeout=3000)
                    page.wait_for_timeout(800)
                    return True
            except Exception:
                continue
        return False

    def _playwright_search(self, query: str) -> list[SearchResult]:
        page = self._ensure_page()
        search_url = _google_search_url(query, self.market)
        try:
            page.goto(search_url, wait_until="domcontentloaded", timeout=60000)
            self._accept_consent(page)
            page.wait_for_load_state("domcontentloaded", timeout=10000)
        except Exception as error:
            raise RuntimeError(f"Playwright could not load Google search: {error}") from error

        deadline = time.monotonic() + 15
        body_text = ""
        while time.monotonic() < deadline:
            try:
                body_text = page.locator("body").inner_text(timeout=3000).lower()
                if page.locator("a:has(h3)").count():
                    break
                if any(marker in body_text for marker in INTERSTITIAL_MARKERS):
                    self._accept_consent(page)
            except Exception:
                pass
            page.wait_for_timeout(500)

        if any(marker in body_text for marker in (
            "unusual traffic", "not a robot", "recaptcha", "необычный трафик", "не робот",
        )):
            raise RuntimeError("Google bot-check blocked the Playwright search.")

        try:
            items = page.locator("a:has(h3)").evaluate_all(
                """anchors => anchors.map(a => {
                    let block = a.closest('.MjjYud, .g, [data-snhf]');
                    if (!block) {
                        let node = a;
                        for (let i = 0; i < 6 && node; i++, node = node.parentElement) {
                            if (node.querySelector && node.querySelector('h3') &&
                                (node.innerText || '').length > (a.innerText || '').length + 20) {
                                block = node; break;
                            }
                        }
                    }
                    return {
                        href: a.getAttribute('href') || a.href || '',
                        text: ((block && block.innerText) || a.innerText || '').trim()
                    };
                })"""
            )
        except Exception as error:
            raise RuntimeError(f"Could not extract Google organic results: {error}") from error

        found: list[SearchResult] = []
        seen: set[str] = set()
        for item in items:
            raw_url = item.get("href", "")
            if raw_url.startswith("/goto?"):
                try:
                    response = self._context.request.get(
                        f"https://www.google.com{raw_url}", timeout=15000
                    )
                    raw_url = response.url
                except Exception:
                    continue
            url = _clean_google_result_url(raw_url)
            if not url or not _is_external_result(url) or url in seen:
                continue
            seen.add(url)
            found.append((url, " ".join((item.get("text") or "").split())))
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


class DuckDuckGoLiteSearchProvider:
    """Keyless HTML fallback with the same URL/title result contract."""

    name = "duckduckgo_lite"

    def __init__(self, market: str = "global") -> None:
        if market not in SUPPORTED_MARKETS:
            raise ValueError(f"Unsupported market: {market}")
        self.market = market

    def search(self, query: str) -> list[SearchResult]:
        global _DUCKDUCKGO_LAST_REQUEST_AT
        remaining = DUCKDUCKGO_MIN_INTERVAL_SECONDS - (
            time.monotonic() - _DUCKDUCKGO_LAST_REQUEST_AT
        )
        if remaining > 0:
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
            with urlopen(request, timeout=20) as response:
                html = response.read().decode("utf-8", errors="replace")
        except Exception as error:
            raise RuntimeError(f"DuckDuckGo Lite search failed: {error}") from error
        lowered = html.lower()
        if any(marker in lowered for marker in DUCKDUCKGO_BLOCK_MARKERS):
            raise RuntimeError("DuckDuckGo bot-check blocked the Lite search.")
        parser = _DuckDuckGoLiteParser()
        parser.feed(html)
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

    def __init__(self, market: str = "global") -> None:
        if market not in SUPPORTED_MARKETS:
            raise ValueError(f"Unsupported market: {market}")
        self.market = market

    def search(self, query: str) -> list[SearchResult]:
        request = Request(
            f"https://search.naver.com/search.naver?{urlencode({'where': 'web', 'query': query})}",
            headers={
                "User-Agent": "Mozilla/5.0",
                "Accept-Language": "en-US,en;q=0.8",
            },
        )
        try:
            with urlopen(request, timeout=20) as response:
                html = response.read().decode("utf-8", errors="replace")
        except Exception as error:
            raise RuntimeError(f"Naver search failed: {error}") from error
        parser = _NaverParser()
        parser.feed(html)
        lowered = html.lower()
        if not parser.results and any(marker in lowered for marker in (
            "captcha", "verify you are a human", "비정상적인 접근",
        )):
            raise RuntimeError("Naver bot-check blocked the search.")
        return parser.results


def _provider_status(error: RuntimeError) -> Literal["blocked", "error"]:
    lowered = (str(error) or error.__class__.__name__).lower()
    return "blocked" if any(marker in lowered for marker in (
        "bot-check", "blocked", "captcha", "recaptcha", "unusual traffic",
    )) else "error"


class ProviderSearchError(RuntimeError):
    """All configured providers failed for a single query."""

    def __init__(self, query: str, attempts: Iterable[ProviderAttempt]) -> None:
        self.query = query
        self.attempts = tuple(attempts)
        detail = self.attempts[-1].message if self.attempts else "No providers configured."
        super().__init__(detail)


class ResilientSearchSession:
    """Try fallback providers only after structured primary failure."""

    def __init__(
        self,
        market: str = "global",
        providers: Iterable[SearchProvider] | None = None,
    ) -> None:
        if market not in SUPPORTED_MARKETS:
            raise ValueError(f"Unsupported market: {market}")
        self.market = market
        self.providers = tuple(providers) if providers is not None else (
            GoogleSearchSession(market),
            DuckDuckGoLiteSearchProvider(market),
            NaverSearchProvider(market),
        )
        self._blocked_providers: set[int] = set()

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
        for index, provider in enumerate(self.providers):
            if index in self._blocked_providers and index < len(self.providers) - 1:
                continue
            try:
                results = tuple(provider.search(query))
            except RuntimeError as error:
                status = _provider_status(error)
                attempts.append(ProviderAttempt(
                    provider=provider.name,
                    query=query,
                    status=status,
                    message=str(error) or error.__class__.__name__,
                    is_fallback=index > 0,
                ))
                if status == "blocked":
                    self._blocked_providers.add(index)
                continue
            attempts.append(ProviderAttempt(
                provider=provider.name,
                query=query,
                status="success",
                result_count=len(results),
                is_fallback=index > 0,
            ))
            # A successful empty response is a real zero-result outcome. It is
            # deliberately not treated as provider failure.
            return ProviderQueryOutcome(results, tuple(attempts))
        return ProviderQueryOutcome((), tuple(attempts))

    def search(self, query: str) -> list[SearchResult]:
        outcome = self.search_with_status(query)
        if outcome.attempts and all(item.status != "success" for item in outcome.attempts):
            raise ProviderSearchError(query, outcome.attempts)
        return list(outcome.results)


def google_search(query: str, market: str = "global") -> list[SearchResult]:
    """Search once, closing a fallback browser afterwards if one was needed."""
    with GoogleSearchSession(market) as session:
        return session.search(query)


def _discovery_issue(query: str, error: RuntimeError) -> DiscoveryIssue:
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


def _search_query(
    searcher: Callable[[str], Iterable[SearchResult] | ProviderQueryOutcome],
    query: str,
) -> tuple[list[SearchResult], list[ProviderAttempt], list[DiscoveryIssue]]:
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
    except RuntimeError as error:
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
        return list(response.results), attempts, issues

    results = list(response)
    provider = _searcher_name(searcher)
    return results, [ProviderAttempt(provider, query, "success", len(results))], []


def _primary_provider_succeeded(attempts: Iterable[ProviderAttempt]) -> bool:
    """Fallback discovery can find sources but cannot establish authority."""
    return any(
        item.status == "success" and not item.is_fallback
        for item in attempts
    )


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
        active_searcher: Callable[[str], Iterable[SearchResult] | ProviderQueryOutcome],
    ) -> DiscoveryOutcome:
        cache_key = (normalize_model(brand), market)
        cached = _OFFICIAL_DOMAIN_CACHE.get(cache_key)
        official_domains = dict(cached or ())
        raw_results: list[SearchResult] = []
        official_results: list[SearchResult] = []
        issues: list[DiscoveryIssue] = []
        provider_attempts: list[ProviderAttempt] = []
        attempted_queries: list[str] = []
        base_queries = build_search_queries(brand, model, article)
        queries: list[str] = []
        if cached is None:
            queries.extend((f"{brand} official website", f"{brand} official {model}"))
        queries.extend(base_queries)
        for query in queries:
            attempted_queries.append(query)
            found, attempts, query_issues = _search_query(active_searcher, query)
            provider_attempts.extend(attempts)
            issues.extend(query_issues)
            raw_results.extend(found)
            if "official" in query and _primary_provider_succeeded(attempts):
                official_results.extend(found)
        if cached is None:
            official_domains = dict(discover_global_official_domains(brand, official_results))
            _OFFICIAL_DOMAIN_CACHE[cache_key] = tuple(official_domains.items())
        relevant_domains = [
            domain for domain in official_domains
            if any(url_belongs_to_domain(url, domain) and model_match(model, f"{title} {url}") == "exact"
                   for url, title in raw_results)
        ]
        site_domains = relevant_domains or list(official_domains)[:1]
        for domain in site_domains[:3]:
            query = f'"{model}" site:{domain}'
            queries.append(query)
            attempted_queries.append(query)
            found, attempts, query_issues = _search_query(active_searcher, query)
            provider_attempts.extend(attempts)
            issues.extend(query_issues)
            raw_results.extend(found)
        ranked_candidates = rank_candidates(
            raw_results, brand, model, article, market=market,
            official_domains=official_domains,
        )
        candidates, rejected_candidates = _partition_relevance_candidates(
            ranked_candidates, brand, model, article,
        )
        if issues and raw_results:
            status: SearchStatus = "partial"
        elif issues:
            status = issues[-1].status
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
    if match == "mismatch":
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
        active_searcher: Callable[[str], Iterable[SearchResult] | ProviderQueryOutcome],
    ) -> DiscoveryOutcome:
        cache_key = (normalize_model(identity.brand), market)
        cached = _OFFICIAL_DOMAIN_CACHE.get(cache_key)
        official_domains = dict(cached or ())
        issues: list[DiscoveryIssue] = []
        provider_attempts: list[ProviderAttempt] = []
        attempted: list[str] = []
        raw_results: list[SearchResult] = []

        if cached is None:
            authority_query = f"{identity.brand} official website"
            attempted.append(authority_query)
            official_results, attempts, query_issues = _search_query(
                active_searcher, authority_query,
            )
            provider_attempts.extend(attempts)
            issues.extend(query_issues)
            authority_results = (
                official_results if _primary_provider_succeeded(attempts) else ()
            )
            official_domains = dict(
                discover_global_official_domains(identity.brand, authority_results)
            )
            _OFFICIAL_DOMAIN_CACHE[cache_key] = tuple(official_domains.items())

        attempted.append(query)
        found, attempts, query_issues = _search_query(active_searcher, query)
        provider_attempts.extend(attempts)
        issues.extend(query_issues)
        raw_results.extend(found)

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
        if issues and raw_results:
            status: SearchStatus = "partial"
        elif issues:
            status = issues[-1].status
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
