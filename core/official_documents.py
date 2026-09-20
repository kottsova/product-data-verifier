"""Official-document discovery: classify links into canonical document types.

A product's manual, datasheet or declaration of conformity is a different
kind of source from its product page: it is never a place to read the
"official product page" from, and it must not be mixed into that group.  This
module recognises such documents in three places (anchors of an official page,
PDF URLs embedded in page state, and search results), classifies them with
multilingual keyword rules, and decides -- with a stated reason -- whether
they belong to the requested model.  Nothing here is brand- or product-specific.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from html.parser import HTMLParser
import re
from typing import Iterable, Literal
from urllib.parse import parse_qsl, unquote, urlencode, urljoin, urlparse, urlunparse

from core.match import normalize_model, normalize_text
from core.sku import requested_sku, sku_relation

DocumentType = Literal[
    "manual", "user_guide", "instruction", "quick_start_guide",
    "datasheet", "spec_sheet", "safety_document", "declaration", "certificate",
]

# Canonical identifier each document type feeds (not yet wired into the CSV flow).
CANONICAL_FIELD: dict[str, str] = {
    "manual": "manual_url",
    "user_guide": "manual_url",
    "instruction": "manual_url",
    "quick_start_guide": "quick_start_guide_url",
    "datasheet": "datasheet_url",
    "spec_sheet": "datasheet_url",
    "safety_document": "safety_document_url",
    "declaration": "declaration_url",
    "certificate": "certificate_url",
}
CANONICAL_DOCUMENT_FIELDS = tuple(dict.fromkeys(CANONICAL_FIELD.values()))

# Order matters: the most specific type wins.
_RULES: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (name, re.compile(pattern, re.IGNORECASE)) for name, pattern in (
        ("quick_start_guide",
         r"quick[\s_-]*start|quick[\s_-]*(?:reference[\s_-]*)?guide|getting[\s_-]*started|start[\s_-]*guide"
         r"|краткое\s+руководство|быстрый\s+старт|kurzanleitung|schnellstart|guide\s+de\s+d[eé]marrage"
         r"|guida\s+rapida|gu[ií]a\s+r[aá]pida|(?:^|[_\s-])qsg(?:$|[_\s.-])"),
        ("declaration",
         r"declaration|conformity|конформ|декларац|konformit|(?:^|[/_\s-])doc[-_]|(?:^|[_\s-])doc(?:$|\.)"),
        ("certificate",
         r"certificat|сертификат|zertifikat|(?:^|[_\s-])cert(?:$|[_\s.-])"),
        ("safety_document",
         r"safety|безопасност|sicherheit|s[eé]curit[eé]|(?:^|[_\s-])m?sds(?:$|[_\s.-])"),
        ("datasheet",
         r"data[\s_-]*sheet|datenblatt|fiche\s+technique|scheda\s+tecnica|ficha\s+t[eé]cnica"
         r"|техническ\w*\s+(?:данн|паспорт|описан)|технический\s+паспорт"),
        ("spec_sheet",
         r"spec(?:ification)?s?[\s_-]*sheet|spec(?:ification)?s?[\s_-]*(?:pdf|document)|"
         r"техническ\w*\s+характеристик"),
        ("user_guide",
         r"user[\s_-]*guide|owner'?s?[\s_-]*guide|руководство\s+пользователя|benutzerhandbuch"
         r"|guide\s+(?:de\s+l.)?utilisateur|gu[ií]a\s+del\s+usuario"),
        ("manual",
         r"manual|bedienungsanleitung|gebrauchsanweisung|manuel|manuale|руководство|паспорт|handbuch"),
        ("instruction",
         r"instruction|инструкц|anleitung|mode\s+d.emploi|istruzioni|instrucciones|leaflet"),
    )
)
_DOCUMENT_EXTENSIONS = (".pdf", ".doc", ".docx")
# Pages that *list* many documents are support hubs, not one document.
_INDEX_HINT = re.compile(r"manuals?\s*(?:and|&|/)\s*faq|manuals?-and-faqs|user-manuals\b|/manuals/?$|downloads?/?$", re.I)


@dataclass(frozen=True, slots=True)
class OfficialDocument:
    url: str
    doc_type: str
    canonical_field: str
    title: str
    authority: str
    model_match: str  # exact | probable | unverified (Stage 34.1: identity checked against the document text)
    reason: str
    source_page: str = ""
    found_on: tuple[str, ...] = ()
    file_type: str = "page"  # pdf | page
    locales: tuple[str, ...] = ()
    identity_evidence: str = ""  # document_text | family_* | not_in_text | ... ("" = not checked)

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class RejectedDocument:
    url: str
    doc_type: str
    title: str
    reason: str
    source_page: str = ""

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def classify_document(url: str, text: str = "") -> str | None:
    """Return a document type for a link, or None when it is not a document."""
    parsed = urlparse(url or "")
    path = unquote(parsed.path or "")
    filename = path.rsplit("/", 1)[-1]
    is_file = path.lower().endswith(_DOCUMENT_EXTENSIONS)
    # Anchor text is the strongest signal; file name next; the rest of the
    # path only for actual files or explicit document folders.
    limit = 100 if is_file else 60
    label = text if len(text or "") <= limit else ""  # a blurb is not a link label
    for haystack in (label, filename if is_file else path.rsplit("/", 1)[-1], path if is_file else ""):
        if not haystack.strip():
            continue
        for name, pattern in _RULES:
            if pattern.search(haystack):
                return name
    return None


def is_document_index(url: str, text: str = "") -> bool:
    return bool(_INDEX_HINT.search(f"{urlparse(url or '').path} {text}"))


def canonical_document_url(url: str) -> str:
    """Canonical key for duplicate merging: drop fragments and cache-busters."""
    parsed = urlparse((url or "").strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return ""
    host = parsed.hostname.lower().removeprefix("www.")
    path = re.sub(r"/{2,}", "/", parsed.path or "/")
    if path != "/":
        path = path.rstrip("/")
    query = urlencode([
        (key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key.lower() not in {"v", "version", "ver", "cache", "cb", "t", "ts", "_"}
        and not key.lower().startswith(("utm_", "_pos", "_sid", "_ss"))
    ])
    return urlunparse((parsed.scheme.lower(), host, path, "", query, ""))


class _AnchorParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.anchors: list[tuple[str, str, str]] = []  # (href, text, title attr)
        self._href = ""
        self._title = ""
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        values = {key.lower(): value or "" for key, value in attrs}
        self._href = values.get("href", "").strip()
        self._title = values.get("title", "") or values.get("aria-label", "")
        self._text = []

    def handle_data(self, data: str) -> None:
        if self._href:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._href:
            self.anchors.append(
                (self._href, " ".join(" ".join(self._text).split()), self._title.strip()),
            )
        if tag == "a":
            self._href = ""
            self._text = []


_URL_BOUNDARY = set("\"'<>\\ \t\r\n()[]{}|^`")


@dataclass(frozen=True, slots=True)
class EmbeddedDocumentLink:
    url: str
    label: str = ""      # description/title of the JSON object that carries the URL
    code: str = ""       # product code stored next to it (e.g. "HX9992_12")
    locale: str = ""     # locale tag (e.g. "DEU") stored next to it, when any


_JSON_FIELD = re.compile(r'"(description|title|name|label|type|code|lang|locale)"\s*:\s*"([^"]{1,120})"')


def _unescape_state(text: str) -> str:
    return (
        text.replace("\\u002F", "/").replace("\\u002f", "/").replace("\\/", "/")
        .replace('\\"', '"').replace("\\u0026", "&")
    )


def scan_embedded_documents(text: str, limit: int = 400) -> list[EmbeddedDocumentLink]:
    """Find document URLs in scripts/JSON state, with the metadata beside them.

    Scanning is linear (no regex backtracking over megabytes of state).  The
    small JSON object around each URL usually names it ("User manual"), its
    product ``code`` and its locale, which anchors alone never provide.
    """
    text = _unescape_state(text or "")
    lowered = text.lower()
    found: dict[str, EmbeddedDocumentLink] = {}
    for extension in _DOCUMENT_EXTENSIONS:
        position = 0
        while len(found) < limit:
            hit = lowered.find(extension, position)
            if hit < 0:
                break
            position = hit + len(extension)
            end = position
            while end < len(text) and text[end] not in _URL_BOUNDARY:
                end += 1
            start = hit
            while start > 0 and text[start - 1] not in _URL_BOUNDARY:
                start -= 1
            candidate = text[start:end]
            if not candidate.startswith(("http://", "https://", "//", "/")) or candidate in found:
                continue
            open_at = text.rfind("{", max(0, start - 420), start)
            window = text[open_at if open_at >= 0 else max(0, start - 240):min(len(text), end + 40)]
            fields: dict[str, str] = {}
            for key, value in _JSON_FIELD.findall(window):
                fields[key] = value
            label = fields.get("description") or fields.get("title") or fields.get("name") or fields.get("label") or ""
            if not label and fields.get("type"):
                label = fields["type"]
            found[candidate] = EmbeddedDocumentLink(
                candidate, label, fields.get("code", ""), fields.get("lang") or fields.get("locale") or "",
            )
    return list(found.values())


def scan_embedded_document_urls(text: str, limit: int = 120) -> list[str]:
    """URLs only (compatibility wrapper around :func:`scan_embedded_documents`)."""
    return [item.url for item in scan_embedded_documents(text, limit)]


def _model_words(model: str, brand: str) -> list[str]:
    brand_key = normalize_model(brand)
    words = []
    for word in re.findall(r"[^\W_]+", model or ""):
        key = normalize_model(word)
        if len(key) >= 2 and key != brand_key:
            words.append(key)
    return words


_VARIANT_WORDS = {
    "FLEX", "NEO", "DUAL", "AIR", "SLIM", "GO", "PRIME", "ELITE", "TURBO", "COMBO", "KIT",
    "LITE", "MINI", "MAX", "PLUS", "PRO", "ULTRA", "XL", "SE", "FE", "S",
}


def _other_identifiers(model: str, text: str) -> list[str]:
    """Distinct model-code-like tokens in ``text`` that are not the requested ones."""
    own = {normalize_model(part) for part in re.findall(r"[^\W_]+", model or "")}
    sku = requested_sku(model)
    # A conflicting identifier looks like a *sibling* of the requested SKU
    # (same letter prefix: HHR32A vs HHR10D).  Unrelated part numbers printed
    # on a manual (W2545G) are not evidence of another model.
    prefix = re.match(r"[A-Z]+", sku.base).group(0) if sku is not None and re.match(r"[A-Z]+", sku.base) else ""
    found: list[str] = []
    for token in re.findall(r"[A-Za-z0-9]+", text or ""):
        key = normalize_model(token)
        if prefix and len(prefix) >= 2 and not key.startswith(prefix):
            continue
        if (
            len(key) >= 5
            and any(ch.isalpha() for ch in key)
            and sum(ch.isdigit() for ch in key) >= 2
            and key not in own
            and not re.fullmatch(r"[0-9]{6,}[A-Z]?|V[0-9]+", key)
            and not re.fullmatch(r"[0-9A-F]{6,}", key)  # hashes / upload ids, not model codes
        ):
            found.append(token)
    return list(dict.fromkeys(found))


def _followed_by_variant_word(words: list[str], haystack_tokens: list[str]) -> bool:
    width = len(words)
    for index in range(len(haystack_tokens) - width + 1):
        if haystack_tokens[index:index + width] == words:
            following = haystack_tokens[index + width] if index + width < len(haystack_tokens) else ""
            if following in _VARIANT_WORDS and following not in words:
                return True
    return False


def document_model_match(
    model: str, brand: str, url: str, text: str, *, linked_from_exact_page: bool,
) -> tuple[str | None, str]:
    """Decide whether a document belongs to the requested model, with a reason."""
    decoded = unquote(urlparse(url).path)
    sku = requested_sku(model)
    relation = sku_relation(model, text, decoded.rsplit("/", 1)[-1])
    if relation.kind == "exact":
        return "exact", "requested model identifier appears in the link text or file name"
    if relation.kind == "regional_suffix":
        return "exact", (
            f"base SKU matches; '-{relation.suffix}' is a regional/market code"
        )
    if relation.kind in {"different_suffix", "different_variant"}:
        return None, (
            f"names a different SKU variant ({relation.evidence or relation.suffix}) "
            "of the same base model"
        )
    others = _other_identifiers(model, f"{text} {decoded.rsplit('/', 1)[-1]}")
    if others:
        return None, f"names a different model identifier ({', '.join(others[:2])})"
    if linked_from_exact_page:
        return "exact", "linked from the exact official product page"
    words = _model_words(model, brand)
    if sku is not None:
        words = [word for word in words if word not in {sku.base, sku.base + sku.suffix, sku.suffix}]
    tokens = [normalize_model(item) for item in re.findall(r"[^\W_]+", normalize_text(f"{text} {decoded}"))]
    if words and len(words) >= 2 and all(word in set(tokens) for word in words):
        if _followed_by_variant_word(words, tokens):
            return None, "model name is extended by a variant word (different product)"
        return "probable", "model family/name words match, requested SKU not stated"
    return None, "requested model is not identified by this document link"


def _same_official_family(url: str, base_url: str, brand: str, official_hosts: Iterable[str]) -> bool:
    host = (urlparse(url).hostname or "").lower().removeprefix("www.")
    base = (urlparse(base_url).hostname or "").lower().removeprefix("www.")
    if not host:
        return False
    return host == base or host in {
        item.lower().removeprefix("www.") for item in official_hosts
    }


def extract_documents(
    html: str,
    base_url: str,
    *,
    brand: str,
    model: str,
    source_authority: str,
    source_is_exact_page: bool,
    official_hosts: Iterable[str] = (),
    source_is_probable_page: bool = False,
    page_title: str = "",
) -> tuple[list[OfficialDocument], list[RejectedDocument]]:
    """Extract and classify documents linked from one *official* page.

    Document links are candidates. HTML support links must stay on an exact
    audited host; linked files still require document identity verification
    before their contents can establish an exact source.
    """
    parser = _AnchorParser()
    parser.feed(html or "")
    links: list[tuple[str, str]] = [(href, text or title) for href, text, title in parser.anchors]
    embedded = {item.url: item for item in scan_embedded_documents(html or "")}
    links.extend((item.url, item.label) for item in embedded.values())
    accepted: list[OfficialDocument] = []
    rejected: list[RejectedDocument] = []
    seen: set[str] = set()
    for href, text in links:
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        url = urljoin(base_url, href.replace("\\/", "/"))
        key = canonical_document_url(url)
        if not key or key in seen:
            continue
        doc_type = classify_document(url, text)
        title = text or unquote(urlparse(url).path.rsplit("/", 1)[-1])
        inferred = ""
        if doc_type is None and urlparse(url).path.lower().endswith(_DOCUMENT_EXTENSIONS):
            # A bare file link on a page titled "... User Manual" is that page's document.
            doc_type = classify_document("", page_title)
            inferred = "; type inferred from the page title" if doc_type else ""
        if doc_type is None:
            if urlparse(url).path.lower().endswith(_DOCUMENT_EXTENSIONS):
                seen.add(key)
                rejected.append(RejectedDocument(
                    url, "unknown", title, "document file whose type cannot be identified",
                    base_url,
                ))
            continue
        seen.add(key)
        if is_document_index(url, text) and not urlparse(url).path.lower().endswith(_DOCUMENT_EXTENSIONS):
            rejected.append(RejectedDocument(
                url, doc_type, title, "document hub/index page, not a model-specific document",
                base_url,
            ))
            continue
        is_file = urlparse(url).path.lower().endswith(_DOCUMENT_EXTENSIONS)
        if not is_file and not _same_official_family(url, base_url, brand, official_hosts):
            rejected.append(RejectedDocument(
                url, doc_type, title, "linked page is on a non-official host", base_url,
            ))
            continue
        record = embedded.get(href)
        match, reason = document_model_match(
            model, brand, url, f"{text} {record.code}" if record and record.code else text,
            linked_from_exact_page=source_is_exact_page and is_file,
        )
        if match is None and is_file and source_is_probable_page and not reason.startswith("names a different"):
            match = "probable"
            reason = "file linked from an official page named for the model family"
        if match is None:
            rejected.append(RejectedDocument(url, doc_type, title, reason, base_url))
            continue
        accepted.append(OfficialDocument(
            url=url,
            doc_type=doc_type,
            canonical_field=CANONICAL_FIELD[doc_type],
            title=title,
            authority=source_authority,
            model_match=match,
            reason=f"{reason}; linked from official page{inferred}",
            source_page=base_url,
            found_on=(base_url,),
            file_type="pdf" if urlparse(url).path.lower().endswith(".pdf") else "page",
            locales=((record.locale,) if record and record.locale else ()),
        ))
    return _collapse_locales(accepted), rejected


_PREFERRED_LOCALES = ("ENG", "AEN", "EN", "EN_GB", "EN-GB", "EN_US", "RUS", "RU")


def _collapse_locales(documents: list[OfficialDocument]) -> list[OfficialDocument]:
    """One entry per (type, title) even when a page lists it in 50 locales."""
    groups: dict[tuple[str, str, str], list[OfficialDocument]] = {}
    passthrough: list[OfficialDocument] = []
    for document in documents:
        if not document.locales:
            passthrough.append(document)
            continue
        groups.setdefault(
            (document.doc_type, normalize_text(document.title), document.source_page), [],
        ).append(document)
    for members in groups.values():
        members.sort(key=lambda item: (
            _PREFERRED_LOCALES.index(item.locales[0].upper())
            if item.locales[0].upper() in _PREFERRED_LOCALES else len(_PREFERRED_LOCALES),
            item.url,
        ))
        chosen = members[0]
        locales = tuple(dict.fromkeys(tag for member in members for tag in member.locales))
        extra = f"; also published in {len(locales) - 1} other locale(s)" if len(locales) > 1 else ""
        passthrough.append(OfficialDocument(**{
            **chosen.to_dict(),
            "reason": f"{chosen.reason}{extra}",
            "locales": locales,
            "found_on": chosen.found_on,
        }))
    return passthrough


def merge_documents(documents: Iterable[OfficialDocument]) -> list[OfficialDocument]:
    """Merge duplicates (same file reached from several pages / cache-busters)."""
    merged: dict[str, OfficialDocument] = {}
    for document in documents:
        key = canonical_document_url(document.url) or document.url
        existing = merged.get(key)
        if existing is None:
            merged[key] = document
            continue
        found = tuple(dict.fromkeys((*existing.found_on, *document.found_on)))
        better = document if (document.model_match == "exact" and existing.model_match != "exact") else existing
        merged[key] = OfficialDocument(**{**better.to_dict(), "found_on": found})
    order = {"exact": 0, "probable": 1}
    return sorted(
        merged.values(),
        key=lambda item: (order.get(item.model_match, 2), item.doc_type, item.url),
    )


def documents_by_canonical_field(
    documents: Iterable[OfficialDocument],
) -> dict[str, list[str]]:
    """Structured canonical identifiers (manual_url, datasheet_url, ...)."""
    fields: dict[str, list[str]] = {name: [] for name in CANONICAL_DOCUMENT_FIELDS}
    for document in documents:
        if document.model_match == "unverified":
            continue  # identity not proven: never a canonical manual/datasheet URL
        urls = fields[document.canonical_field]
        if document.url not in urls:
            urls.append(document.url)
    return fields
