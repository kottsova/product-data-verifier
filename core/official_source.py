"""Stage 31.4 official-source priority: gate telemetry, prose facts, images.

Pure functions over data the pipeline already produced. Nothing here searches
or fetches, and nothing re-ranks evidence: source authority is still decided
by ``core.validation`` (verified manufacturer/official_document = rank 4 >
specialized reference = rank 3). This module adds

* the mandatory Official Source Resolution Gate record (why an official page
  was or was not used, in the fixed vocabulary the operator asked for);
* a generic prose-fact extractor for verified exact-model official pages that
  state specs in marketing copy instead of a spec table;
* redirect-away detection so an official product URL that bounced to a
  category page is not accepted as exact-model evidence;
* official image and auxiliary-link collection.

No brand, domain, or product answer is hardcoded here.
"""

from __future__ import annotations

from collections import Counter
import re
from typing import Iterable, Mapping
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from core.discovery import canonicalize_url, url_belongs_to_domain, _registrable_domain
from core.extract import RawAttribute
from core.identity import ProductIdentity, base_model_in_text
from core.match import candidate_model_match


OFFICIAL_SOURCE_TYPES = frozenset({"manufacturer", "official_document"})

FAILURE_DOMAIN_NOT_FOUND = "domain_not_found"
FAILURE_DOMAIN_UNVERIFIED = "domain_verification_failed"
FAILURE_EXACT_MODEL_NOT_FOUND = "exact_model_not_found"
FAILURE_INACCESSIBLE = "inaccessible"
FAILURE_FETCH_FAILED = "fetch_failed"
FAILURE_EXTRACTION_FAILED = "extraction_failed"
FAILURE_IDENTITY_REJECTED = "identity_rejected"
FAILURE_NO_CANONICAL = "no_usable_canonical_attributes"

REDIRECTED_AWAY_REASON = "redirected_away_from_exact_page"
PROSE_FACT_CONTEXT = "verified official exact-model page prose"


def is_official(item: Mapping[str, object]) -> bool:
    return (
        item.get("authority_status") == "verified"
        and item.get("source_type") in OFFICIAL_SOURCE_TYPES
    )


def _is_exact_official_candidate(item: Mapping[str, object]) -> bool:
    return (
        is_official(item)
        and item.get("model_match") == "exact"
        and item.get("identity_relation") in {"same_base_model", "exact_variant"}
    )


def _yes_no(value: bool) -> str:
    return "yes" if value else "no"


def _url_path_key(url: str) -> tuple[str, str]:
    parsed = urlparse(canonicalize_url(url) or url)
    return parsed.hostname or "", parsed.path.rstrip("/").casefold()


# ---------------------------------------------------------------------------
# Redirect-away detection
# ---------------------------------------------------------------------------


def _identity_texts(source: Mapping[str, object]) -> list[str]:
    soup = BeautifulSoup(str(source.get("html") or ""), "html.parser")
    texts = [
        str(source.get("final_url") or ""),
        soup.title.get_text(" ", strip=True) if soup.title else "",
    ]
    texts.extend(h.get_text(" ", strip=True) for h in soup.find_all("h1", limit=3))
    for selector in ('meta[property="og:title"]', 'meta[name="twitter:title"]'):
        tag = soup.select_one(selector)
        if tag and tag.get("content"):
            texts.append(str(tag.get("content")))
    return texts


def redirected_away_from_exact_page(
    source: Mapping[str, object],
    candidate: Mapping[str, object],
    identity: ProductIdentity,
) -> bool:
    """True when an official exact-model URL landed on a different page that
    does not itself identify the model (typically a category/landing page)."""
    if source.get("status") != "success" or not is_official(candidate):
        return False
    requested = str(candidate.get("url") or source.get("source_url") or "")
    final = str(source.get("final_url") or "")
    if not requested or not final:
        return False
    if _url_path_key(requested) == _url_path_key(final):
        return False
    model = identity.base_model or identity.commercial_model
    if not model:
        return False
    return not base_model_in_text(model, " ".join(_identity_texts(source)))


# ---------------------------------------------------------------------------
# Prose facts from an exact-model official page
# ---------------------------------------------------------------------------

_DROP_TAGS = ("script", "style", "noscript", "template", "svg", "head")

_SIZE_RE = re.compile(
    r"(?P<n>\d{1,2}(?:\.\d{1,2})?)\s?(?:[\"”″]|-?\s?inch(?:es)?\b)\s+"
    r"(?:[A-Za-z]+\s+){0,2}?(?:display|screen)\b",
    re.IGNORECASE,
)
_REFRESH_RE = re.compile(r"(?P<up>up to\s+)?(?P<n>\d{2,3})\s?Hz\b", re.IGNORECASE)
_RAM_RE = re.compile(r"(?P<n>\d{1,3})\s?GB\s+(?:of\s+)?(?:RAM|memory)\b", re.IGNORECASE)
_CHIP_RE = re.compile(
    r"\b(?P<chip>(?:[A-Z][\w®™\-]*|\d[\w\-]*)(?:\s(?:[A-Z][\w®™\-.]*|\d[\w\-.]*)){0,3})"
    r"\s(?:chip|processor|chipset|SoC)\b"
)
_REAR_ROLES = ("main", "wide", "ultrawide", "ultra-wide", "telephoto", "periscope", "macro", "rear")
_FRONT_ROLES = ("front", "selfie")
_CAMERA_RE = re.compile(
    r"(?P<mp>\d{1,3}(?:\.\d)?)\s?MP\s+(?P<role>"
    + "|".join(_REAR_ROLES + _FRONT_ROLES)
    + r")\b(?:\s+(?:camera|lens))?",
    re.IGNORECASE,
)
_CHIP_STOP = {"the", "new", "a", "an", "our", "its", "with", "and", "powered", "by"}


def _page_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(list(_DROP_TAGS)):
        tag.decompose()
    return re.sub(r"\s+", " ", soup.get_text(" ", strip=True))


def _model_tail(model: str) -> str:
    words = re.findall(r"[A-Za-z0-9]+", model)
    return words[-1].casefold() if words else ""


def _preceding_words(text: str, start: int, count: int = 3) -> list[str]:
    return re.findall(r"[A-Za-z0-9]+", text[max(0, start - 40):start])[-count:]


def _resolve_sibling_values(
    matches: list[tuple[str, int]],
    text: str,
    model: str,
) -> str | None:
    """One value per attribute, or None.

    A marketing page for a model family often quotes sibling variants
    ("Pro 6.3\" ... Pro XL 6.8\""). With several distinct values, accept one
    only if the words right before it name this model (they end with the
    model's own final word) and no other distinct value does.
    """
    values = list(dict.fromkeys(value for value, _ in matches))
    if len(values) == 1:
        return values[0]
    tail = _model_tail(model)
    named = [
        value for value, position in matches
        if tail and (_preceding_words(text, position, 1) or [""])[-1].casefold() == tail
    ]
    named_values = list(dict.fromkeys(named))
    return named_values[0] if len(named_values) == 1 else None


def _strip_chip(value: str) -> str:
    words = value.split()
    while words and words[0].casefold() in _CHIP_STOP:
        words.pop(0)
    return " ".join(words)


def extract_official_prose_facts(
    source: Mapping[str, object],
    identity: ProductIdentity,
    already_present: Iterable[str] = (),
) -> list[RawAttribute]:
    """Spec facts stated in prose on a verified exact-model official page.

    Same evidence gate as identity materialization: the page itself must be
    fetched, authority-verified, exact-model, and contain the model phrase.
    Emitted labels are the schema's own aliases, so mapping and validation
    treat them like any other official fact; nothing is inferred.
    """
    if source.get("status") != "success" or not is_official(source):
        return []
    if source.get("model_relevance") != "exact_base_model":
        return []
    if source.get("identity_relation") not in {"same_base_model", "exact_variant"}:
        return []
    model = identity.base_model or identity.commercial_model
    html = str(source.get("html") or "")
    if not model or not html:
        return []
    text = _page_text(html)
    if not base_model_in_text(model, text):
        return []

    skip = {item.casefold() for item in already_present}
    url = str(source.get("final_url") or source.get("source_url") or "")
    facts: list[tuple[str, str, str | None, str]] = []

    def window(position: int, end: int) -> str:
        return text[max(0, position - 60):end + 60]

    size = _resolve_sibling_values(
        [(m.group("n"), m.start()) for m in _SIZE_RE.finditer(text)], text, model,
    )
    if size and "display_size" not in skip:
        facts.append(("Display size", size, "in", f'{size}" display'))

    refresh = [
        (m.group("n"), m.start()) for m in _REFRESH_RE.finditer(text)
        if re.search(r"display|screen|refresh", text[max(0, m.start() - 160):m.start()], re.I)
    ]
    rate = _resolve_sibling_values(refresh, text, model)
    if rate and "refresh_rate" not in skip:
        first = next(m for m in _REFRESH_RE.finditer(text) if m.group("n") == rate)
        facts.append(("Refresh rate", rate, "Hz", first.group(0)))

    chips = [
        (_strip_chip(m.group("chip")), m.start()) for m in _CHIP_RE.finditer(text)
        if re.search(r"\d", _strip_chip(m.group("chip")))
    ]
    chip = _resolve_sibling_values(chips, text, model)
    if chip and "processor" not in skip:
        facts.append(("Processor", chip, None, f"{chip} chip"))

    ram = _resolve_sibling_values(
        [(m.group("n"), m.start()) for m in _RAM_RE.finditer(text)], text, model,
    )
    if ram and "ram" not in skip:
        facts.append(("RAM", ram, "GB", f"{ram} GB RAM"))

    rear: list[str] = []
    front: list[str] = []
    for m in _CAMERA_RE.finditer(text):
        role = m.group("role").casefold().replace("ultra-wide", "ultrawide")
        entry = f"{m.group('mp')} MP {role}"
        bucket = front if role in _FRONT_ROLES else rear
        if entry not in bucket:
            bucket.append(entry)
    if rear and "rear_camera" not in skip:
        joined = " + ".join(rear)
        facts.append(("Rear camera", joined, None, joined))
    if front and "front_camera" not in skip:
        facts.append(("Front camera", front[0].replace(" front", "").replace(" selfie", ""),
                      None, front[0]))

    return [
        RawAttribute(
            name=name,
            value=value,
            unit=unit,
            source_url=url,
            source_type=str(source.get("source_type") or "manufacturer"),
            evidence=f"Official exact-model page states: {snippet!r}.",
            extraction_method="plain_text",
            confidence="medium",
            raw_value=f"{value} {unit}" if unit else value,
            attribute_kind="product",
            context=PROSE_FACT_CONTEXT,
        )
        for name, value, unit, snippet in facts
    ]


# ---------------------------------------------------------------------------
# Images and auxiliary links
# ---------------------------------------------------------------------------

_IMAGE_EXT_RE = re.compile(r"\.(?:jpe?g|png|webp|avif)(?:$|[?#=&])", re.IGNORECASE)
_IMAGE_CDN_RE = re.compile(
    r"(?:googleusercontent\.com|ggpht\.com|cloudfront\.net|akamaized\.net|"
    r"scene7\.com|images?\.|/images?/|/media/|/assets?/)",
    re.IGNORECASE,
)
_UNSAFE_IMAGE_WORDS = re.compile(
    r"logo|icon|sprite|banner|favicon|avatar|badge|flag|placeholder|default-user|"
    r"pixel\.gif|tracking|1x1|arrow|chevron|button|thumb(?:nail)?s?/|/ogw/|promo",
    re.IGNORECASE,
)
_SIZE_HINT_RE = re.compile(r"[=,/_-](?:s|w|h)(\d{2,5})(?=$|[-_,/&?#=]|-[a-z])")
MIN_IMAGE_EDGE = 400
MAX_IMAGE_EDGE = 2048  # Telegram rejects URL photos with width + height > 10000
MAX_PRODUCT_IMAGES = 10
# Below this many official photos the album is topped up from a secondary
# reference page (official photos always stay first).
MIN_OFFICIAL_PHOTOS = 4


def _srcset_urls(value: str) -> list[str]:
    return [part.strip().split(" ")[0] for part in value.split(",") if part.strip()]


def _image_edge_hint(url: str) -> int | None:
    hints = [int(item) for item in _SIZE_HINT_RE.findall(url)]
    return max(hints) if hints else None


def _image_key(url: str) -> str:
    # Sized CDN variants of one picture ("<id>=s3000-w3000") collapse to <id>;
    # other URLs collapse to their canonical form.
    if "=" in url.split("?")[0].rsplit("/", 1)[-1] or "googleusercontent" in url:
        return url.split("=")[0]
    return canonicalize_url(url) or url


def _clamp_image_edge(url: str, edge: int | None) -> str | None:
    """Keep a size-hinted URL within MAX_IMAGE_EDGE (or drop it).

    Image CDNs that encode the rendition in the URL ("=w3000", "=s3000-w3000")
    can be asked for a smaller one by rewriting the number; any other host's
    oversized variant is dropped rather than guessed at.
    """
    if edge is None or edge <= MAX_IMAGE_EDGE:
        return url
    host = urlparse(url).hostname or ""
    if host.endswith(("googleusercontent.com", "ggpht.com")):
        return _SIZE_HINT_RE.sub(
            lambda m: m.group(0).replace(m.group(1), str(MAX_IMAGE_EDGE))
            if int(m.group(1)) > MAX_IMAGE_EDGE else m.group(0),
            url,
        )
    return None


def extract_official_image_records(
    source: Mapping[str, object],
    model: str | None = None,
    *,
    official: bool = True,
) -> list[tuple[str, bool]]:
    """(url, names_the_model) pairs for product photos on an official page.

    Only https URLs that look like raster photos are kept; logos, icons,
    banners, tracking pixels, avatars and small renditions are dropped, and
    sized variants of one picture are deduplicated (largest kept). The flag is
    True when the image's own alt text names the model.
    """
    if source.get("status") != "success" or is_official(source) != official:
        return []
    if source.get("identity_relation") not in {"same_base_model", "exact_variant"}:
        return []
    html = str(source.get("html") or "")
    if not html:
        return []
    base = str(source.get("final_url") or source.get("source_url") or "")
    soup = BeautifulSoup(html, "html.parser")
    found: list[tuple[str, str]] = []  # (url, hint text)
    for meta in soup.find_all("meta"):
        name = str(meta.get("property") or meta.get("name") or "").casefold()
        if name in {"og:image", "og:image:url", "twitter:image"} and meta.get("content"):
            found.append((str(meta["content"]), "og"))
    for tag in soup.find_all(["img", "source"]):
        alt = str(tag.get("alt") or "")
        if tag.name == "source":
            # <source> has no alt of its own; its <picture>'s <img> does.
            picture = tag.find_parent("picture")
            picture_img = picture.find("img") if picture else None
            alt = str(picture_img.get("alt") or "") if picture_img else ""
        hint = " ".join(filter(None, (
            alt,
            " ".join(tag.get("class") or ()),
            str(tag.get("id") or ""),
            " ".join(" ".join(p.get("class") or ()) for p in tag.find_parents(limit=3)),
        )))
        for attribute in ("src", "data-src", "data-lazy-src"):
            if tag.get(attribute):
                found.append((str(tag[attribute]), hint))
        for attribute in ("srcset", "data-srcset"):
            for candidate in _srcset_urls(str(tag.get(attribute) or "")):
                found.append((candidate, hint))

    best: dict[str, tuple[int, str]] = {}
    relevant: dict[str, bool] = {}
    order: list[str] = []
    for raw_url, hint in found:
        url = urljoin(base, raw_url.strip())
        parsed = urlparse(url)
        if parsed.scheme != "https" or not parsed.hostname:
            continue
        if parsed.path.casefold().endswith((".svg", ".gif", ".ico")):
            continue
        if not (_IMAGE_EXT_RE.search(url) or _IMAGE_CDN_RE.search(url)):
            continue
        if _UNSAFE_IMAGE_WORDS.search(f"{url} {hint}"):
            continue
        edge = _image_edge_hint(url)
        if edge is not None and edge < MIN_IMAGE_EDGE:
            continue
        clamped = _clamp_image_edge(url, edge)
        if clamped is None:
            continue
        url = clamped
        edge = _image_edge_hint(url)
        key = _image_key(url)
        score = edge or 0
        relevant[key] = relevant.get(key, False) or bool(
            model and hint.strip() and base_model_in_text(model, hint)
        )
        if not official:
            # A secondary page carries far more page furniture than an
            # official one: only an image whose own alt text names the model
            # or the page's declared share image (og/twitter) is a product photo.
            if hint != "og" and not relevant[key]:
                relevant.pop(key, None)
                continue
            if hint == "og":
                relevant[key] = True
        if key not in best:
            order.append(key)
            best[key] = (score, url)
        elif score > best[key][0]:
            best[key] = (score, url)
    return [(best[key][1], relevant[key]) for key in order]


def extract_official_images(
    source: Mapping[str, object],
    model: str | None = None,
) -> list[str]:
    """Product photo URLs of one official page, model-naming images first."""
    records = extract_official_image_records(source, model)
    records.sort(key=lambda item: not item[1])
    return [url for url, _ in records][:MAX_PRODUCT_IMAGES]


def select_product_images(
    sources: Iterable[Mapping[str, object]],
    model: str | None = None,
) -> list[str]:
    """Deduplicated official product photos across pages, best first.

    Images whose alt text names the model rank ahead of unlabeled ones; within
    a rank the page order (official, exact-model pages first) is preserved.
    """
    chosen: dict[str, tuple[str, bool]] = {}
    for source in sources:
        for url, named in extract_official_image_records(source, model):
            key = _image_key(url)
            if key not in chosen or (named and not chosen[key][1]):
                chosen[key] = (url, named)
    ranked = sorted(chosen.values(), key=lambda item: not item[1])
    return [url for url, _ in ranked][:MAX_PRODUCT_IMAGES]


def select_product_image_records(
    sources: Iterable[Mapping[str, object]],
    model: str | None = None,
) -> list[dict[str, str]]:
    """Bounded, deduplicated photo records ``{"url", "role", "source"}``.

    Official photos first (model-naming ones ahead of unlabeled ones). Only
    when fewer than ``MIN_OFFICIAL_PHOTOS`` official photos exist is the album
    topped up, up to ``MAX_PRODUCT_IMAGES``, with photos from exact-model
    secondary reference pages, each labeled ``secondary``.
    """
    sources = list(sources)
    seen: set[str] = set()
    records: list[dict[str, str]] = []

    def collect(official: bool) -> None:
        chosen: dict[str, tuple[str, bool, str]] = {}
        for source in sources:
            if not official and source.get("source_type") != "specialized_reference":
                continue
            page = str(source.get("final_url") or source.get("source_url") or "")
            for url, named in extract_official_image_records(source, model, official=official):
                key = _image_key(url)
                if key in seen:
                    continue
                if key not in chosen or (named and not chosen[key][1]):
                    chosen[key] = (url, named, page)
        for url, _named, page in sorted(chosen.values(), key=lambda item: not item[1]):
            if len(records) >= MAX_PRODUCT_IMAGES:
                break
            seen.add(_image_key(url))
            records.append({"url": url, "role": "official" if official else "secondary", "source": page})

    collect(True)
    if len(records) < MIN_OFFICIAL_PHOTOS:
        collect(False)
    return records


_AUXILIARY_KINDS = {
    # Singular "Review" is the product's own review; plural "Reviews" is the
    # site-wide reviews index and is not product-specific.
    "review": "review",
    "pictures": "pictures", "pics": "pictures",
    "opinions": "opinions", "read all opinions": "opinions",
    "compare": "compare",
    "prices": "prices", "price": "prices",
}


def extract_auxiliary_links(source: Mapping[str, object]) -> list[dict[str, str]]:
    """Navigation links a spec aggregator offers besides its spec table.

    Review / Pictures / Opinions / Compare / Prices are stored as separate
    auxiliary links, never as attributes. "Related devices" is dropped on
    purpose: this marketplace bot does not surface it.
    """
    if source.get("status") != "success" or source.get("document_type") != "html":
        return []
    html = str(source.get("html") or "")
    base = str(source.get("final_url") or source.get("source_url") or "")
    if not html or not base:
        return []
    links: dict[str, dict[str, str]] = {}
    for anchor in BeautifulSoup(html, "html.parser").find_all("a", href=True):
        label = re.sub(r"\s+", " ", anchor.get_text(" ", strip=True)).casefold()
        kind = _AUXILIARY_KINDS.get(label)
        href = str(anchor["href"]).strip()
        if not kind or not href or href.startswith(("#", "javascript:")):
            continue
        url = urljoin(base, href)
        if not url.startswith("https://"):
            continue
        # A page repeats these labels (top nav and product tabs); keep the most
        # specific URL, which is the product-scoped one.
        if kind not in links or len(url) > len(links[kind]["url"]):
            links[kind] = {"kind": kind, "url": url, "source": base}
    return list(links.values())


# ---------------------------------------------------------------------------
# Linked spec pages of an accepted official page
# ---------------------------------------------------------------------------

_SPEC_LINK_TEXT_RE = re.compile(
    r"^(?:tech(?:nical)?\s+specs?|specifications?|specs|full\s+specs?|all\s+specs?)$", re.I,
)
_SPEC_LINK_PATH_RE = re.compile(r"(?:^|[-_/])(?:tech-?)?specs?(?:ifications?)?$", re.I)
MAX_SPEC_LINKS = 2


def extract_official_spec_links(
    source: Mapping[str, object],
    identity: ProductIdentity,
    limit: int = MAX_SPEC_LINKS,
) -> list[str]:
    """Same-domain "Tech Specs" links of an accepted exact-model official page.

    Search providers are unreliable at surfacing an official product's spec
    page, but the product page itself links to it (an Overview / Tech Specs
    tab pair is the norm). A link qualifies only when it stays on the page's
    own registrable domain, reads as a specs link by its label or its path,
    and its path itself names the model -- so a generic "specs" link to some
    other product or a site-wide page is never followed.
    """
    if source.get("status") != "success" or not is_official(source):
        return []
    if source.get("identity_relation") not in {"same_base_model", "exact_variant"}:
        return []
    html = str(source.get("html") or "")
    base = str(source.get("final_url") or source.get("source_url") or "")
    model = identity.base_model or identity.commercial_model
    if not html or not base or not model:
        return []
    domain = _registrable_domain(urlparse(base).hostname or "")
    own = canonicalize_url(base)
    found: list[str] = []
    for anchor in BeautifulSoup(html, "html.parser").find_all("a", href=True):
        href = str(anchor["href"]).strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        url = urljoin(base, href).split("#", 1)[0]
        parsed = urlparse(url)
        if parsed.scheme != "https" or not url_belongs_to_domain(url, domain):
            continue
        label = re.sub(r"\s+", " ", anchor.get_text(" ", strip=True))
        path = parsed.path.rstrip("/")
        if not (_SPEC_LINK_TEXT_RE.match(label) or _SPEC_LINK_PATH_RE.search(path)):
            continue
        # The path must name *this* model: a sibling that merely extends it
        # ("<model>-xl-specs") is a different product's page.
        if candidate_model_match(model, "", url) != "exact":
            continue
        canonical = canonicalize_url(url)
        if not canonical or canonical == own or url in found:
            continue
        if any(canonicalize_url(item) == canonical for item in found):
            continue
        found.append(url)
        if len(found) >= limit:
            break
    return found


# ---------------------------------------------------------------------------
# Official Source Resolution Gate
# ---------------------------------------------------------------------------


_UNUSABLE_VALUE_RE = re.compile(r"[぀-ヿ㐀-䶿一-鿿가-힣豈-﫿]")


def _usable_value(value: object) -> bool:
    """A canonical value worth showing: not foreign-script page furniture."""
    text = str(value if value is not None else "")
    return bool(text.strip()) and len(text) <= 200 and not _UNUSABLE_VALUE_RE.search(text)


def _fetch_state(source: Mapping[str, object]) -> str:
    if source.get("status") == "success":
        return "success"
    blocked = source.get("blocked_reason")
    if blocked:
        return f"blocked:{blocked}"
    return f"error:{str(source.get('error') or source.get('status') or 'unknown')[:80]}"


def build_official_resolution(
    *,
    identity: ProductIdentity,
    candidates: Iterable[Mapping[str, object]],
    rejected_candidates: Iterable[Mapping[str, object]] = (),
    selected: Iterable[Mapping[str, object]] = (),
    fetched_sources: Iterable[Mapping[str, object]] = (),
    raw_attributes: Iterable[RawAttribute] = (),
    canonical_attributes: Iterable[object] = (),
    schema_names: Iterable[str] | None = None,
    provider_attempts: Iterable[object] = (),
) -> dict[str, object]:
    """The eight-field Official Source Resolution Gate record.

    Computed from what discovery, fetch, extraction and mapping actually
    produced, so it explains an unused official source with evidence rather
    than a guess.
    """
    candidates = list(candidates)
    selected_urls = {canonicalize_url(str(c.get("url") or "")) for c in selected}
    fetched = list(fetched_sources)
    attempts = list(provider_attempts)

    verified = [c for c in candidates if is_official(c)]
    exact = [c for c in verified if _is_exact_official_candidate(c)]

    domain: str | None = None
    if verified:
        domain = _registrable_domain(urlparse(str(verified[0]["url"])).hostname or "")
    else:
        for attempt in attempts:
            accepted = getattr(attempt, "accepted_official_url", None)
            if accepted:
                domain = _registrable_domain(urlparse(accepted).hostname or "")
                break
    if domain is None:
        brand_label = re.sub(r"[^a-z0-9]", "", identity.brand.casefold())
        for c in (*candidates, *rejected_candidates):
            host = urlparse(str(c.get("url") or "")).hostname or ""
            if brand_label and re.sub(r"[^a-z0-9]", "", _registrable_domain(host).split(".")[0]) == brand_label:
                domain = _registrable_domain(host)
                break

    def official_domain_fetch(source: Mapping[str, object]) -> bool:
        host = urlparse(str(source.get("final_url") or source.get("source_url") or "")).hostname or ""
        return bool(domain and url_belongs_to_domain(f"https://{host}/", domain)) and is_official(source)

    official_fetches = [s for s in fetched if official_domain_fetch(s)]
    exact_urls = {canonicalize_url(str(c["url"])) for c in exact}
    exact_fetches = [
        s for s in official_fetches
        if canonicalize_url(str(s.get("source_url") or "")) in exact_urls
        or s.get("identity_relation") in {"same_base_model", "exact_variant"}
    ]
    rejected_fetches = [s for s in exact_fetches if s.get("official_rejection_reason")]
    usable_fetches = [
        s for s in exact_fetches
        if s.get("status") == "success" and not s.get("official_rejection_reason")
    ]
    usable_urls = {
        canonicalize_url(str(s.get("source_url") or s.get("final_url") or ""))
        for s in usable_fetches
    }
    raw_count = sum(
        1 for a in raw_attributes if canonicalize_url(a.source_url) in usable_urls
    )
    allowed = None if schema_names is None else set(schema_names)
    canonical_urls = Counter(
        canonicalize_url(str(getattr(item, "source_url", "")))
        for item in canonical_attributes
        if (allowed is None or getattr(item, "canonical_name", None) in allowed)
        and _usable_value(getattr(item, "value", ""))
    )
    canonical_count = sum(canonical_urls[u] for u in usable_urls)

    domain_accessible = bool(
        official_fetches and any(s.get("status") == "success" for s in official_fetches)
    ) or any(
        getattr(a, "candidate_count", 0) for a in attempts
        if getattr(a, "provider", "") in {"direct_domain_probe", "browser_official_discovery"}
    ) or bool(verified)

    exact_found = bool(exact)
    page_accessible = bool(usable_fetches)

    if page_accessible:
        fetch_status = "success"
    elif rejected_fetches:
        fetch_status = "success_but_" + str(rejected_fetches[0]["official_rejection_reason"])
    elif exact_fetches:
        fetch_status = _fetch_state(exact_fetches[0])
    elif exact_found:
        fetch_status = "not_attempted"
    else:
        fetch_status = "not_applicable"

    reason: str | None = None
    detail: str | None = None
    if not domain:
        reason, detail = FAILURE_DOMAIN_NOT_FOUND, "no official domain candidate was discovered"
    elif not verified:
        reason = FAILURE_DOMAIN_UNVERIFIED
        detail = f"{domain} was a candidate but no authority evidence verified it"
    elif not exact_found:
        reason = FAILURE_EXACT_MODEL_NOT_FOUND
        detail = f"no exact-model page found within the verified official ecosystem {domain}"
    elif not page_accessible:
        if rejected_fetches:
            reason = FAILURE_IDENTITY_REJECTED
            detail = str(rejected_fetches[0]["official_rejection_reason"])
        elif exact_fetches:
            state = _fetch_state(exact_fetches[0])
            blocked = state.startswith("blocked:") or "403" in state or "captcha" in state
            reason = FAILURE_INACCESSIBLE if blocked else FAILURE_FETCH_FAILED
            detail = state
        else:
            reason = FAILURE_FETCH_FAILED
            detail = (
                "exact official page was discovered but not fetched "
                + ("(outside fetch slots/budget)" if not (selected_urls & exact_urls) else "(budget exhausted)")
            )
    elif raw_count == 0:
        reason, detail = FAILURE_EXTRACTION_FAILED, "official page fetched but yielded no attributes"
    elif canonical_count == 0:
        reason = FAILURE_NO_CANONICAL
        detail = (
            f"{raw_count} raw official attributes, none mapped to a usable "
            "canonical schema field"
        )

    return {
        "official_domain_candidate": domain,
        "official_domain_verified": _yes_no(bool(verified)),
        "official_domain_accessible": _yes_no(domain_accessible),
        "official_exact_product_page_found": _yes_no(exact_found),
        "official_product_page_accessible": _yes_no(page_accessible),
        "official_fetch_status": fetch_status,
        "official_attributes_extracted_count": raw_count,
        "official_failure_reason": reason,
        "official_failure_detail": detail,
        "official_canonical_attributes_count": canonical_count,
        "official_exact_page_urls": [str(c["url"]) for c in exact][:5],
    }


def source_priority(urls_with_types: Iterable[tuple[str, str | None, str | None]]) -> list[dict[str, str]]:
    """Official sources first, then secondary; stable within each group."""
    seen: dict[str, dict[str, str]] = {}
    for url, source_type, authority in urls_with_types:
        if not url or url in seen:
            continue
        official = source_type in OFFICIAL_SOURCE_TYPES and authority in (None, "verified")
        seen[url] = {"url": url, "role": "official" if official else "secondary"}
    return sorted(seen.values(), key=lambda item: item["role"] != "official")
