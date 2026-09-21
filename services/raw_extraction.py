"""Stage 34 extraction service: DiscoveryDebugResult -> raw official attributes.

The service *consumes* a discovery result.  It never searches: it does not
import discovery providers, does not look at dealer/secondary/rejected groups,
and fetches only the official product pages, official support pages and
official documents that discovery already selected.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from io import BytesIO
import time
from typing import Callable
from urllib.parse import urlparse

from core.official_documents import OfficialDocument
from core.authority_registry import find_seed
from core.identity import assess_product_page_identity
from core.page_inspection import fetch_working_page, inspect_product_page
from core.raw_extraction import (
    RawAttribute,
    extract_document_attributes,
    extract_html_attributes,
)
from services.discovery_debug import DiscoveryDebugResult, DiscoverySource

HtmlFetcher = Callable[[str], "tuple[str, str] | None"]
BytesFetcher = Callable[[str], "tuple[str, bytes, str] | None"]  # (final_url, body, content_type)

_USABLE_MATCH = {"exact", "probable"}
_MAX_PAGES = 8
_MAX_DOCUMENTS = 8
_MAX_PDF_BYTES = 25_000_000
_MAX_PDF_PAGES = 200
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36"
    ),
    "Accept": "*/*",
}


@dataclass(frozen=True, slots=True)
class SourceExtraction:
    url: str
    final_url: str
    source_type: str            # official_product_page | official_support_page | official_document
    model_match: str
    fetched: bool
    fetch_issue: str = ""
    document_type: str = ""
    identity_relation: str = "unknown"
    identity_evidence: str = ""
    authority_status: str = "unknown"
    authority_evidence_url: str = ""
    authority_evidence_kind: str = "none"
    authority_evidence_excerpt: str = ""
    authority_checked_on: str = ""
    authority_rules_version: int = 0
    operator_relation: str = "unknown"
    authority_scope: str = "unknown"
    spec_location: str = ""     # inspection of delivered HTML (pages only)
    requires_interaction: str = ""
    js_shell: bool = False
    attributes: tuple[RawAttribute, ...] = ()
    excluded: dict[str, int] = field(default_factory=dict)
    stats: dict[str, object] = field(default_factory=dict)
    issues: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        data = asdict(self)
        data["attributes"] = [item.to_dict() for item in self.attributes]
        return data


@dataclass(frozen=True, slots=True)
class ExtractionResult:
    product_name: str
    brand: str
    model: str
    discovery_status: str
    pages: tuple[SourceExtraction, ...]
    support_pages: tuple[SourceExtraction, ...]
    documents: tuple[SourceExtraction, ...]
    skipped: tuple[dict[str, str], ...]
    runtime_seconds: float
    search_calls: int = 0       # always 0: extraction never searches

    @property
    def sources(self) -> tuple[SourceExtraction, ...]:
        return (*self.pages, *self.support_pages, *self.documents)

    @property
    def attributes(self) -> tuple[RawAttribute, ...]:
        return tuple(a for source in self.sources for a in source.attributes)

    def to_dict(self) -> dict[str, object]:
        return {
            "product_name": self.product_name, "brand": self.brand, "model": self.model,
            "discovery_status": self.discovery_status, "runtime_seconds": self.runtime_seconds,
            "search_calls": self.search_calls,
            "pages": [s.to_dict() for s in self.pages],
            "support_pages": [s.to_dict() for s in self.support_pages],
            "documents": [s.to_dict() for s in self.documents],
            "skipped": list(self.skipped),
            "raw_attribute_count": len(self.attributes),
        }


# Stage 35 reads this object; the alias names the contract ("raw extraction result") without a new type.
RawExtractionResult = ExtractionResult


def _default_bytes_fetch(url: str) -> tuple[str, bytes, str] | None:
    import requests

    try:
        response = requests.get(url, headers=_HEADERS, timeout=40, allow_redirects=True, stream=True)
    except requests.RequestException:
        return None
    try:
        if response.status_code >= 400:
            return None
        chunks, size = [], 0
        for chunk in response.iter_content(128 * 1024):
            chunks.append(chunk)
            size += len(chunk)
            if size >= _MAX_PDF_BYTES:
                break
        return response.url, b"".join(chunks), str(response.headers.get("content-type", "")).lower()
    finally:
        response.close()


def _pdf_pages(body: bytes) -> list[str]:
    from pypdf import PdfReader

    reader = PdfReader(BytesIO(body))
    pages: list[str] = []
    for page in list(reader.pages)[:_MAX_PDF_PAGES]:
        try:
            pages.append(page.extract_text() or "")
        except Exception:  # noqa: BLE001 - one unreadable page must not lose the document
            pages.append("")
    return pages


class RawExtractionService:
    """Extract raw official attributes from what discovery already found."""

    def __init__(self, fetch_html: HtmlFetcher | None = None, fetch_bytes: BytesFetcher | None = None) -> None:
        self._fetch_html = self._retrying(fetch_html or (lambda url: fetch_working_page(url, timeout=25.0)))
        self._fetch_bytes = self._retrying(fetch_bytes or _default_bytes_fetch)

    @staticmethod
    def _retrying(fetch: Callable, attempts: int = 3):
        """Official sites intermittently drop or rate-limit a request; one miss is not a verdict."""
        def wrapped(url: str):
            for attempt in range(attempts):
                result = fetch(url)
                if result is not None:
                    return result
                time.sleep(1.5 * (attempt + 1))
            return None
        return wrapped

    # -- pages -----------------------------------------------------------
    def _extract_page(self, source: DiscoverySource, source_type: str, model: str, brand: str) -> SourceExtraction:
        provenance = dict(
            authority_status=source.authority_status,
            authority_evidence_url=source.authority_evidence_url,
            authority_evidence_kind=source.authority_evidence_kind,
            authority_evidence_excerpt=source.authority_evidence_excerpt,
            authority_checked_on=source.authority_checked_on,
            authority_rules_version=source.authority_rules_version,
            operator_relation=source.operator_relation,
            authority_scope=source.authority_scope,
        )
        page = self._fetch_html(source.url)
        if page is None:
            return SourceExtraction(
                url=source.url, final_url="", source_type=source_type, model_match=source.model_match,
                fetched=False, fetch_issue="page could not be fetched (HTTP error, block or timeout)",
                issues=("fetch_failed",), **provenance,
            )
        final_url, html = page
        source_host = source.domain.lower().removeprefix("www.")
        final_host = (urlparse(final_url).hostname or "").lower().removeprefix("www.")
        redirect_seed = find_seed(brand, final_host, category=source.product_category) if source_host != final_host else None
        if source_host != final_host and not (redirect_seed and redirect_seed.first_party):
            return SourceExtraction(
                url=source.url, final_url=final_url, source_type=source_type,
                model_match="rejected", fetched=True,
                issues=("authority_redirect_not_verified",),
                authority_status="unknown", authority_evidence_kind="none",
            )
        inspection = inspect_product_page(html, final_url)
        if source_type in {"official_product_page", "official_support_page"}:
            decision = assess_product_page_identity(model, html, final_url)
            if decision.relation != "exact":
                return SourceExtraction(
                    url=source.url, final_url=final_url, source_type=source_type,
                    model_match="rejected", fetched=True, identity_relation=decision.relation,
                    identity_evidence=decision.evidence,
                    issues=("product_identity_not_verified",), **provenance,
                )
        extraction, stats = extract_html_attributes(html, final_url, model, source_type)
        issues: list[str] = []
        if not extraction.attributes:
            if inspection.spec_location == "js_required" or inspection.js_shell:
                issues.append("js_required: specifications are not in the delivered HTML")
            else:
                issues.append(f"no raw attributes in delivered HTML (spec_location={inspection.spec_location})")
        elif inspection.requires_interaction == "true":
            issues.append("js_required: part of the specifications needs interaction (not performed)")
        stats["html_bytes"] = len(html)
        return SourceExtraction(
            url=source.url, final_url=final_url, source_type=source_type, model_match=source.model_match,
            fetched=True, spec_location=inspection.spec_location, requires_interaction=inspection.requires_interaction,
            identity_relation="exact",
            identity_evidence=decision.evidence,
            js_shell=inspection.js_shell, attributes=tuple(extraction.attributes), excluded=dict(extraction.excluded),
            stats=stats, issues=tuple(issues), **provenance,
        )

    # -- documents -------------------------------------------------------
    def _extract_document(self, document: OfficialDocument, model: str, brand: str) -> SourceExtraction:
        is_pdf = document.file_type == "pdf" or urlparse(document.url).path.lower().endswith(".pdf")
        if not is_pdf:
            if urlparse(document.url).path.lower().endswith((".doc", ".docx")):
                return SourceExtraction(
                    url=document.url, final_url="", source_type="official_document", model_match=document.model_match,
                    fetched=False, fetch_issue="unsupported document format", document_type=document.doc_type,
                    issues=("unsupported_format",),
                )
            page = self._fetch_html(document.url)
            if page is None:
                return SourceExtraction(
                    url=document.url, final_url="", source_type="official_document", model_match=document.model_match,
                    fetched=False, fetch_issue="document page could not be fetched", document_type=document.doc_type,
                    issues=("fetch_failed",),
                )
            final_host = (urlparse(page[0]).hostname or "").lower().removeprefix("www.")
            original_host = (urlparse(document.url).hostname or "").lower().removeprefix("www.")
            redirect_seed = find_seed(brand, final_host, category="unknown") if final_host != original_host else None
            if final_host != original_host and not (redirect_seed and redirect_seed.first_party):
                return SourceExtraction(
                    url=document.url, final_url=page[0], source_type="official_document",
                    model_match="rejected", fetched=True, document_type=document.doc_type,
                    issues=("authority_redirect_not_verified",),
                )
            identity = assess_product_page_identity(model, page[1], page[0])
            if identity.relation != "exact":
                return SourceExtraction(
                    url=document.url, final_url=page[0], source_type="official_document",
                    model_match="rejected", fetched=True, document_type=document.doc_type,
                    identity_relation=identity.relation, identity_evidence=identity.evidence,
                    issues=("document_identity_not_verified",),
                )
            extraction, stats = extract_html_attributes(page[1], page[0], model, "official_document", document.doc_type)
            return SourceExtraction(
                url=document.url, final_url=page[0], source_type="official_document", model_match=document.model_match,
                fetched=True, document_type=document.doc_type, attributes=tuple(extraction.attributes),
                identity_relation="exact", identity_evidence=identity.evidence,
                excluded=dict(extraction.excluded), stats=stats,
                issues=() if extraction.attributes else ("no raw attributes in document page",),
            )
        fetched = self._fetch_bytes(document.url)
        if fetched is None:
            return SourceExtraction(
                url=document.url, final_url="", source_type="official_document", model_match=document.model_match,
                fetched=False, fetch_issue="document could not be downloaded", document_type=document.doc_type,
                issues=("fetch_failed",),
            )
        final_url, body, _content_type = fetched
        final_host = (urlparse(final_url).hostname or "").lower().removeprefix("www.")
        original_host = (urlparse(document.url).hostname or "").lower().removeprefix("www.")
        redirect_seed = find_seed(brand, final_host, category="unknown") if final_host != original_host else None
        if final_host != original_host and not (redirect_seed and redirect_seed.first_party):
            return SourceExtraction(
                url=document.url, final_url=final_url, source_type="official_document",
                model_match="rejected", fetched=True, document_type=document.doc_type,
                issues=("authority_redirect_not_verified",),
            )
        try:
            pages = _pdf_pages(body)
        except Exception as error:  # noqa: BLE001
            return SourceExtraction(
                url=document.url, final_url=final_url, source_type="official_document", model_match=document.model_match,
                fetched=True, fetch_issue=f"pdf unreadable: {type(error).__name__}", document_type=document.doc_type,
                issues=("pdf_unreadable",),
            )
        if not any(page.strip() for page in pages):
            return SourceExtraction(
                url=document.url, final_url=final_url, source_type="official_document", model_match=document.model_match,
                fetched=True, document_type=document.doc_type, stats={"pages": len(pages), "text_chars": 0},
                issues=("pdf_has_no_text_layer (scanned/image PDF, OCR not attempted)",),
            )
        extraction, report = extract_document_attributes(
            pages, final_url, model, document.doc_type, document.model_match,
        )
        issues: list[str] = []
        if not extraction.attributes:
            issues.append("no raw attributes in document text")
        if report.get("multi_model_document"):
            issues.append(f"multi-model document: neighbouring identifiers {report.get('sibling_examples')} kept out")
        if not report.get("model_in_text"):
            issues.append(
                "document does not name the requested model: attributes withheld "
                "(discovery matched it by URL/title only)"
            )
        report["text_chars"] = sum(len(page) for page in pages)
        return SourceExtraction(
            url=document.url, final_url=final_url, source_type="official_document", model_match=document.model_match,
            fetched=True, document_type=document.doc_type, attributes=tuple(extraction.attributes),
            excluded=dict(extraction.excluded), stats=report, issues=tuple(issues),
        )

    # -- entry point -----------------------------------------------------
    def extract(self, discovery: DiscoveryDebugResult) -> ExtractionResult:
        started = time.monotonic()
        model = discovery.model
        skipped: list[dict[str, str]] = []

        def pick(sources: tuple[DiscoverySource, ...], label: str) -> list[DiscoverySource]:
            usable = [s for s in sources if s.model_match in _USABLE_MATCH]
            for source in sources:
                if source.model_match not in _USABLE_MATCH:
                    skipped.append({"url": source.url, "kind": label, "reason": f"model_match={source.model_match}"})
            usable.sort(key=lambda s: s.model_match != "exact")
            for source in usable[_MAX_PAGES:]:
                skipped.append({"url": source.url, "kind": label, "reason": "page cap"})
            return usable[:_MAX_PAGES]

        product = pick(discovery.official_pages, "product_page")
        support = pick(discovery.support_pages, "support_page")
        documents = [d for d in discovery.documents if d.model_match in _USABLE_MATCH][:_MAX_DOCUMENTS]
        for doc in discovery.documents:
            if doc.model_match not in _USABLE_MATCH:
                skipped.append({"url": doc.url, "kind": "document", "reason": f"model_match={doc.model_match}"})

        with ThreadPoolExecutor(max_workers=4) as pool:
            page_futures = [pool.submit(self._extract_page, s, "official_product_page", model, discovery.brand) for s in product]
            support_futures = [pool.submit(self._extract_page, s, "official_support_page", model, discovery.brand) for s in support]
            doc_futures = [pool.submit(self._extract_document, d, model, discovery.brand) for d in documents]
            pages = tuple(f.result() for f in page_futures)
            support_pages = tuple(f.result() for f in support_futures)
            docs = tuple(f.result() for f in doc_futures)
        return ExtractionResult(
            product_name=discovery.product_name, brand=discovery.brand, model=model,
            discovery_status=discovery.status, pages=pages, support_pages=support_pages, documents=docs,
            skipped=tuple(skipped), runtime_seconds=round(time.monotonic() - started, 2),
        )
