"""Stage 33 discovery-only application service.

This boundary deliberately exposes only source discovery: official product
pages, official support pages, official documents (manual, datasheet,
declaration, ...) and discovery metadata about how a page hides its
specifications.  It never extracts, validates, exports or localizes values.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date
from difflib import SequenceMatcher
from dataclasses import asdict, dataclass, field, replace
import re
import threading
import time
from typing import Callable, Iterable, Literal
from urllib.parse import unquote, urlparse

from bs4 import BeautifulSoup

from core.budget import WallClockBudget
from core.authority import TrustedSource, resolve_authority
from core.authority_registry import RULES_VERSION, find_seed
from core.discovery import (
    Candidate,
    DiscoveryOutcome,
    DiscoveryRuntimeConfig,
    ResilientSearchSession,
    _page_kind,
    _search_result_record,
    canonicalize_url,
    discover_with_status,
)
from core.document_identity import DocumentReader, read_pdf_text, verify_documents
from core.identity import assess_product_page_identity
from core.official_documents import (
    CANONICAL_FIELD,
    OfficialDocument,
    RejectedDocument,
    classify_document,
    document_model_match,
    documents_by_canonical_field,
    extract_documents,
    is_document_index,
    merge_documents,
)
from core.page_inspection import PageInspection, fetch_working_page, inspect_product_page
from core.sku import requested_sku, sku_relation


DiscoveryGroup = Literal["official", "dealer", "secondary", "rejected"]
DisplayMatch = Literal["exact", "probable", "weak", "rejected"]

_DOCUMENT_SUFFIXES = (".pdf", ".doc", ".docx")
_OFFICIAL_SURFACE_PROVIDERS = frozenset({"direct_domain_probe", "browser_official_discovery"})
_MANUAL_TYPES = frozenset({"manual", "user_guide", "instruction", "quick_start_guide"})


@dataclass(frozen=True, slots=True)
class DiscoverySource:
    url: str
    domain: str
    source_type: str
    title: str
    model_match: DisplayMatch
    authority: str
    reason: str
    group: DiscoveryGroup
    sku_relation: str = ""
    sku_suffix: str = ""
    page_role: str = "product"  # product | support | document
    content_identity_verified: bool = False
    identity_evidence: str = ""
    authority_status: str = "unknown"
    authority_evidence_url: str = ""
    authority_evidence_kind: str = "none"
    authority_evidence_excerpt: str = ""
    authority_checked_on: str = ""
    authority_rules_version: int = 0
    operator_relation: str = "unknown"
    authority_scope: str = "unknown"
    product_category: str = "unknown"

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class DiscoveryDebugResult:
    product_name: str
    brand: str
    model: str
    market: str
    status: Literal["PASS", "PARTIAL", "FAIL"]
    exact_official_found: bool
    official: tuple[DiscoverySource, ...]
    dealers: tuple[DiscoverySource, ...]
    secondary: tuple[DiscoverySource, ...]
    rejected: tuple[DiscoverySource, ...]
    runtime_seconds: float
    search_status: str
    attempted_queries: tuple[str, ...]
    providers: tuple[str, ...]
    provider_failures: tuple[dict[str, object], ...]
    candidate_counts: dict[str, int]
    # ---- Stage 33.1 -------------------------------------------------------
    official_pages: tuple[DiscoverySource, ...] = ()
    support_pages: tuple[DiscoverySource, ...] = ()
    documents: tuple[OfficialDocument, ...] = ()
    rejected_documents: tuple[RejectedDocument, ...] = ()
    document_fields: dict[str, list[str]] = field(default_factory=dict)
    page_inspections: tuple[PageInspection, ...] = ()
    discovery_metadata: dict[str, object] = field(default_factory=dict)
    official_paths: tuple[dict[str, object], ...] = ()
    trace: dict[str, object] = field(default_factory=dict)
    # ---- Stage 33.2 -------------------------------------------------------
    page_metadata: tuple[dict[str, object], ...] = ()
    sku_rejections: tuple[dict[str, str], ...] = ()
    performance: dict[str, object] = field(default_factory=dict)
    page_fetches: tuple[dict[str, object], ...] = ()

    def to_dict(self) -> dict[str, object]:
        data = asdict(self)
        for name in ("official", "dealers", "secondary", "rejected", "official_pages", "support_pages"):
            data[name] = [item.to_dict() for item in getattr(self, name)]
        data["documents"] = [item.to_dict() for item in self.documents]
        data["rejected_documents"] = [item.to_dict() for item in self.rejected_documents]
        data["page_inspections"] = [item.to_dict() for item in self.page_inspections]
        return data


def parse_discovery_product_name(text: str) -> tuple[str, str] | None:
    """Resolve a name-only input into a conservative brand/model split.

    Pipe syntax remains available for arbitrary multi-word brands.  Plain text
    handles leading ``The`` brands and ampersand brand names generically; all
    other names follow the bot's established first-token brand convention.
    """
    normalized = " ".join((text or "").split())
    if not normalized:
        return None
    if "|" in normalized:
        parts = [" ".join(part.split()) for part in normalized.split("|")]
        if len(parts) >= 2 and parts[0] and parts[1]:
            return parts[0], parts[1]
        return None
    tokens = normalized.split()
    if len(tokens) < 2:
        return None
    brand_width = 1
    if tokens[0].casefold() == "the" and len(tokens) >= 3:
        brand_width = 2
    elif len(tokens) >= 4 and tokens[1] == "&":
        brand_width = 3
    return " ".join(tokens[:brand_width]), " ".join(tokens[brand_width:])


def _display_match(candidate: Candidate, *, rejected: bool = False) -> DisplayMatch:
    if rejected:
        return "rejected"
    relation = str(candidate.get("relevance_relation") or "")
    if relation == "exact":
        return "exact"
    if relation == "likely_variant":
        return "probable"
    return "weak"


def _authority(candidate: Candidate) -> tuple[str, DiscoveryGroup]:
    source_type = str(candidate.get("source_type") or "other")
    verified = candidate.get("authority_status") == "verified"
    if verified and source_type == "official_document":
        return "official support", "official"
    if verified and source_type == "manufacturer":
        return "manufacturer", "official"
    if verified and source_type == "distributor":
        return "independent distributor", "dealer"
    if verified and source_type == "retailer":
        return "authorized dealer", "dealer"
    return "secondary", "secondary"


def _page_role(url: str, title: str) -> str:
    """Product page, support page, or document -- never mixed."""
    path = urlparse(url).path.lower()
    if any(part in path.split("/") for part in ("forum", "forums", "discussions", "community")):
        return "forum"
    if path.endswith(_DOCUMENT_SUFFIXES):
        return "document"
    if classify_document(url, title) is not None and not is_document_index(url, title):
        return "document"
    if _page_kind(url) == "support" or is_document_index(url, title):
        return "support"
    return "product"


def _candidate_view(candidate: Candidate, *, rejected: bool = False) -> DiscoverySource:
    authority, group = _authority(candidate)
    role = _page_role(str(candidate.get("url") or ""), str(candidate.get("title") or ""))
    if role == "forum" and group == "official":
        group = "secondary"
        authority = "official-host community content"
    if rejected:
        group = "rejected"
    reasons = list(candidate.get("relevance_reasons") or ())
    authority_reason = str(candidate.get("authority_reason") or "").strip()
    if authority_reason:
        reasons.append(authority_reason)
    if not reasons:
        reasons.append(str(candidate.get("product_match_evidence") or "candidate retained"))
    url = str(candidate.get("url") or "")
    title = str(candidate.get("title") or "")
    return DiscoverySource(
        url=url,
        domain=str(candidate.get("domain") or ""),
        source_type=str(candidate.get("source_type") or "other"),
        title=title,
        model_match=_display_match(candidate, rejected=rejected),
        authority=authority,
        reason="; ".join(dict.fromkeys(reason for reason in reasons if reason)),
        group=group,
        sku_relation=str(candidate.get("sku_relation") or ""),
        sku_suffix=str(candidate.get("sku_suffix") or ""),
        page_role=role,
        authority_status=str(candidate.get("authority_status") or "unknown"),
        authority_evidence_url=str(candidate.get("authority_evidence_url") or ""),
        authority_evidence_kind=str(candidate.get("authority_evidence_kind") or "none"),
        authority_evidence_excerpt=str(candidate.get("authority_evidence_excerpt") or ""),
        authority_checked_on=str(candidate.get("authority_checked_on") or ""),
        authority_rules_version=int(candidate.get("authority_rules_version") or 0),
        operator_relation=str(candidate.get("operator_relation") or "unknown"),
        authority_scope=str(candidate.get("authority_scope") or "unknown"),
        product_category=str(candidate.get("product_category") or "unknown"),
    )


def _official_hosts(candidates: Iterable[Candidate]) -> list[str]:
    hosts: list[str] = []
    for candidate in candidates:
        if candidate.get("authority_status") == "verified" and candidate.get("source_type") in {
            "manufacturer", "official_document",
        }:
            host = str(candidate.get("domain") or "").lower().removeprefix("www.")
            if host and host not in hosts:
                hosts.append(host)
    return hosts


def _first_party_seed(brand: str, url: str, product_category: str = "unknown") -> bool:
    seed = find_seed(brand, urlparse(url).hostname or "", category=product_category)
    return bool(seed and seed.first_party)


def _document_from_candidate(
    source: DiscoverySource, brand: str, model: str,
) -> tuple[OfficialDocument | None, RejectedDocument | None]:
    doc_type = classify_document(source.url, source.title)
    if doc_type is None:
        # A PDF is not a document merely because it is a PDF.
        return None, RejectedDocument(
            source.url, "unknown", source.title, "document purpose is not identified (not a manual/datasheet/declaration/...)",
        )
    match, reason = document_model_match(
        model, brand, source.url, source.title, linked_from_exact_page=False,
    )
    if match is None and source.model_match == "exact":
        match, reason = "exact", "search result carries the requested model identifier"
    if match is None:
        return None, RejectedDocument(source.url, doc_type, source.title, reason)
    return OfficialDocument(
        url=source.url,
        doc_type=doc_type,
        canonical_field=CANONICAL_FIELD[doc_type],
        title=source.title or unquote(urlparse(source.url).path.rsplit("/", 1)[-1]),
        authority=source.authority,
        model_match=match,
        reason=f"{reason}; found on verified official domain {source.domain}",
        source_page=source.url,
        found_on=(source.url,),
        file_type="pdf" if urlparse(source.url).path.lower().endswith(".pdf") else "page",
    ), None


def _verify_document_identity(
    documents: list[OfficialDocument], brand: str, model: str, reader: DocumentReader,
) -> list[OfficialDocument]:
    """Stage 34.1: a link from an official page is context; ``exact`` needs identity in the document itself."""
    verdicts = verify_documents(documents, model, brand, reader)
    if not verdicts:
        return documents
    checked = []
    for document in documents:
        verdict = verdicts.get(document.url)
        if verdict is None:
            checked.append(document)
            continue
        checked.append(replace(
            document, model_match=verdict.match, identity_evidence=verdict.evidence,
            reason=f"{document.reason}; identity check: {verdict.reason}",
        ))
    return merge_documents(checked)


def _metadata_from_inspections(
    inspections: list[tuple[DiscoverySource, PageInspection]],
) -> dict[str, object]:
    """Aggregate per-page diagnostics, preferring the exact product page."""
    if not inspections:
        return {
            "inspected_pages": 0, "has_expandable_specs": False,
            "has_hidden_spec_content": "unknown", "requires_interaction": "unknown",
            "primary_page": "",
        }
    ordered = sorted(inspections, key=lambda item: (item[0].model_match != "exact",))
    source, primary = ordered[0]

    def merged(name: str) -> str:
        values = [getattr(inspection, name) for _, inspection in ordered]
        for preferred in ("true", "unknown", "false"):
            if preferred in values:
                return preferred
        return "unknown"

    return {
        "inspected_pages": len(inspections),
        "primary_page": primary.url or source.url,
        "has_expandable_specs": primary.has_expandable_specs,
        "has_hidden_spec_content": primary.has_hidden_spec_content,
        "requires_interaction": primary.requires_interaction,
        "any_page_requires_interaction": merged("requires_interaction"),
        "signals": list(primary.signals),
        "spec_controls": list(primary.spec_controls),
    }


def _result_from_outcome(
    product_name: str,
    brand: str,
    model: str,
    market: str,
    outcome: DiscoveryOutcome,
    runtime_seconds: float,
    *,
    fetch: Callable[[str], tuple[str, str] | None] | None = None,
    extra_results: Iterable[object] = (),
    document_reader: DocumentReader | None = None,
    product_category: str = "unknown",
) -> DiscoveryDebugResult:
    grouped: dict[DiscoveryGroup, list[DiscoverySource]] = {
        "official": [], "dealer": [], "secondary": [], "rejected": [],
    }
    for candidate in outcome.candidates:
        view = _candidate_view(candidate)
        grouped[view.group].append(view)
    for candidate in outcome.rejected_candidates:
        grouped["rejected"].append(_candidate_view(candidate, rejected=True))

    known_rejected_urls = {item.url for item in grouped["rejected"]}
    for entry in outcome.trace.entries:
        if entry.outcome != "rejected" or not entry.canonical_url:
            continue
        if entry.canonical_url in known_rejected_urls:
            continue
        known_rejected_urls.add(entry.canonical_url)
        domain = entry.canonical_url.split("/", 3)[2] if "/" in entry.canonical_url else ""
        grouped["rejected"].append(DiscoverySource(
            entry.canonical_url, domain, "other", entry.title, "rejected",
            "secondary", entry.reason, "rejected",
        ))

    official_all = grouped["official"]
    official_pages = [item for item in official_all if item.page_role == "product"]
    support_pages = [item for item in official_all if item.page_role == "support"]
    document_sources = [item for item in official_all if item.page_role == "document"]

    documents: list[OfficialDocument] = []
    rejected_documents: list[RejectedDocument] = []
    for source in document_sources:
        document, rejection = _document_from_candidate(source, brand, model)
        if document is not None:
            documents.append(document)
        if rejection is not None:
            rejected_documents.append(rejection)

    inspections: list[tuple[DiscoverySource, PageInspection]] = []
    html_document_decisions: dict[str, object] = {}
    if fetch is not None:
        product_decisions: dict[str, object] = {}
        support_decisions: dict[str, object] = {}
        targets: list[tuple[DiscoverySource, str]] = []
        rank = {"exact": 0, "probable": 1, "weak": 2}
        for source in sorted(official_pages, key=lambda item: rank.get(item.model_match, 3))[:6]:
            targets.append((source, "product"))
        for source in [*support_pages, *document_sources]:
            if len([t for t in targets if t[1] == "scan"]) >= 3:
                break
            if not source.url.lower().split("?")[0].endswith(_DOCUMENT_SUFFIXES):
                targets.append((source, "scan"))
        # Search-level official claims are only leads. Inspect a bounded set
        # of their pages so an independently anchored relationship can be
        # resolved without granting trust from the snippet itself.
        provisional = [
            item for item in grouped["secondary"]
            if item.authority_status == "provisional"
            and item.page_role in {"product", "support"}
        ]
        targets.extend((item, "authority") for item in provisional[:4])

        def load(target: tuple[DiscoverySource, str]):
            return target, fetch(target[0].url)

        with ThreadPoolExecutor(max_workers=max(1, min(4, len(targets)))) as pool:
            fetched = list(pool.map(load, targets)) if targets else []
        official_hosts = _official_hosts(outcome.candidates)
        for (source, kind), page in fetched:
            if page is None:
                continue
            final_url, html = page
            if kind in {"product", "scan"} and not _first_party_seed(brand, final_url, product_category):
                # A redirect can leave the audited host. The destination is
                # a fresh authority decision, never inherited from the URL.
                continue
            if kind == "product":
                inspections.append((source, inspect_product_page(html, final_url)))
                product_decisions[source.url] = assess_product_page_identity(model, html, final_url)
                if product_decisions[source.url].relation != "exact":
                    continue
            elif kind == "scan" and source.page_role == "support":
                support_decisions[source.url] = assess_product_page_identity(model, html, final_url)
                if support_decisions[source.url].relation in {"different_variant", "related_item"}:
                    continue
            elif kind == "scan" and source.page_role == "document":
                html_document_decisions[source.url] = assess_product_page_identity(model, html, final_url)
                if html_document_decisions[source.url].relation != "exact":
                    continue
            elif kind == "authority" and source.page_role == "product":
                product_decisions[source.url] = assess_product_page_identity(model, html, final_url)
                continue
            elif kind == "authority":
                continue
            title_match = re.search(r"<title[^>]*>(.*?)</title>", html[:200_000], re.I | re.S)
            page_title = " ".join((title_match.group(1) if title_match else "").split())
            found, rejected_links = extract_documents(
                html, final_url, brand=brand, model=model,
                source_authority="official (linked from verified official page)",
                source_is_exact_page=(source.model_match == "exact" and kind == "product"),
                source_is_probable_page=(source.model_match in {"exact", "probable"}),
                official_hosts=official_hosts, page_title=page_title,
            )
            documents.extend(found)
            rejected_documents.extend(rejected_links)
        # Links are for manual checking: show the spelling that actually loads
        # (canonicalisation drops trailing slashes some sites 404 without).
        working = {source.url: page[0] for (source, _), page in fetched if page}
        reviewed_pages: list[DiscoverySource] = []
        for item in official_pages:
            final_url = working.get(item.url, item.url)
            if final_url != item.url and not _first_party_seed(brand, final_url, product_category):
                grouped["secondary"].append(replace(
                    item, url=final_url, group="secondary", authority="unknown",
                    authority_status="unknown", authority_evidence_kind="none",
                    model_match="weak", reason=f"{item.reason}; redirected outside audited host",
                ))
                continue
            decision = product_decisions.get(item.url)
            if decision is None:
                reviewed_pages.append(replace(
                    item, model_match="weak", reason=f"{item.reason}; product content not verified",
                ))
                continue
            if decision.relation == "exact":
                reviewed_pages.append(replace(
                    item, url=working.get(item.url, item.url), model_match="exact",
                    content_identity_verified=True, identity_evidence=decision.evidence,
                ))
            elif decision.relation == "unknown":
                reviewed_pages.append(replace(
                    item, url=working.get(item.url, item.url), model_match="weak",
                    content_identity_verified=False, identity_evidence=decision.evidence,
                    reason=f"{item.reason}; product identity not established: {decision.evidence}",
                ))
            else:
                grouped["rejected"].append(replace(
                    item, url=working.get(item.url, item.url), model_match="rejected",
                    group="rejected", reason=f"{item.reason}; {decision.evidence}",
                    identity_evidence=decision.evidence,
                ))
        official_pages = reviewed_pages
        reviewed_support: list[DiscoverySource] = []
        for item in support_pages:
            final_url = working.get(item.url, item.url)
            if final_url != item.url and not _first_party_seed(brand, final_url, product_category):
                grouped["secondary"].append(replace(
                    item, url=final_url, group="secondary", authority="unknown",
                    authority_status="unknown", authority_evidence_kind="none",
                    model_match="weak", reason=f"{item.reason}; redirected outside audited host",
                ))
                continue
            decision = support_decisions.get(item.url)
            if decision is None or decision.relation == "unknown":
                reviewed_support.append(replace(item, url=final_url, model_match="weak"))
            elif decision.relation == "exact":
                reviewed_support.append(replace(
                    item, url=final_url, model_match="exact", content_identity_verified=True,
                    identity_evidence=decision.evidence,
                ))
            else:
                grouped["rejected"].append(replace(
                    item, url=final_url, group="rejected", model_match="rejected",
                    identity_evidence=decision.evidence,
                    reason=f"{item.reason}; {decision.evidence}",
                ))
        support_pages = reviewed_support
        trusted = tuple(
            TrustedSource(domain=urlparse(page[0]).hostname or "", html=page[1], url=page[0])
            for (source, _kind), page in fetched
            if page and source.authority_status == "verified"
            and source.source_type == "manufacturer"
            and _first_party_seed(brand, page[0], product_category)
        )
        for (source, kind), page in fetched:
            if kind != "authority" or page is None:
                continue
            final_url, html = page
            final_host = urlparse(final_url).hostname or ""
            assessment = resolve_authority(html, BeautifulSoup(html, "html.parser").get_text(" ", strip=True), final_host, brand, trusted)
            if assessment.role == "manufacturer":
                decision = product_decisions.get(source.url)
                if source.page_role == "product" and (decision is None or decision.relation != "exact"):
                    continue
                promoted = replace(
                    source, url=final_url, group="official", authority="manufacturer",
                    authority_status="verified", authority_evidence_url=assessment.corroboration.evidence_url or "",
                    authority_evidence_kind="contextual_anchor",
                    authority_evidence_excerpt=assessment.corroboration.evidence_excerpt,
                    authority_checked_on=date.today().isoformat(), authority_rules_version=RULES_VERSION,
                    operator_relation=assessment.corroboration.relation,
                    model_match="exact" if decision else source.model_match,
                    content_identity_verified=bool(decision),
                    identity_evidence=decision.evidence if decision else "",
                    reason=f"{source.reason}; {assessment.reason}",
                )
                grouped["secondary"].remove(source)
                (official_pages if source.page_role == "product" else support_pages).append(promoted)
            elif assessment.role in {"official_distributor", "authorized_dealer"}:
                grouped["secondary"].remove(source)
                grouped["dealer"].append(replace(
                    source, url=final_url, group="dealer", authority=assessment.role,
                    authority_status="verified", authority_evidence_url=assessment.corroboration.evidence_url or "",
                    authority_evidence_kind="contextual_anchor",
                    authority_evidence_excerpt=assessment.corroboration.evidence_excerpt,
                    authority_checked_on=date.today().isoformat(), authority_rules_version=RULES_VERSION,
                    operator_relation=assessment.corroboration.relation,
                    reason=f"{source.reason}; {assessment.reason}",
                ))
        grouped["official"] = [*official_pages, *support_pages, *document_sources]
        official_all = grouped["official"]

    # Document links surfaced by dedicated document queries.
    official_hosts = _official_hosts(outcome.candidates)
    for record in extra_results:
        url = str(getattr(record, "url", "") or "")
        title = str(getattr(record, "title", "") or "")
        canonical = canonicalize_url(url)
        if not canonical:
            continue
        doc_type = classify_document(url, title)
        if doc_type is None:
            continue
        host = (urlparse(canonical).hostname or "").lower().removeprefix("www.")
        if host not in official_hosts:
            rejected_documents.append(RejectedDocument(
                canonical, doc_type, title, "document host is not a verified official domain",
            ))
            continue
        match, reason = document_model_match(model, brand, canonical, title, linked_from_exact_page=False)
        if match is None:
            rejected_documents.append(RejectedDocument(canonical, doc_type, title, reason))
            continue
        documents.append(OfficialDocument(
            url=canonical, doc_type=doc_type, canonical_field=CANONICAL_FIELD[doc_type],
            title=title or canonical, authority="official (verified manufacturer domain)",
            model_match=match, reason=f"{reason}; returned by a document-focused query on an official domain",
            source_page=canonical, found_on=(canonical,),
            file_type="pdf" if urlparse(canonical).path.lower().endswith(".pdf") else "page",
        ))

    merged_documents = merge_documents(documents)
    # A title/URL or link from an exact product page is a document lead.
    # HTML manuals need their own primary-object content before "exact".
    merged_documents = [
        replace(
            document, model_match="unverified",
            identity_evidence="HTML document content not verified",
            reason=f"{document.reason}; HTML document content not verified",
        ) if document.file_type != "pdf" and document.model_match == "exact"
        and (document.url not in html_document_decisions or html_document_decisions[document.url].relation != "exact")
        else document
        for document in merged_documents
    ]
    if document_reader is not None:
        merged_documents = _verify_document_identity(merged_documents, brand, model, document_reader)
    exact_official = any(item.model_match == "exact" for item in official_pages)
    if exact_official:
        status: Literal["PASS", "PARTIAL", "FAIL"] = "PASS"
    elif official_all or merged_documents or any(item.model_match == "exact" for item in grouped["secondary"]):
        status = "PARTIAL"
    else:
        status = "FAIL"

    attempts = outcome.provider_attempts
    providers = tuple(dict.fromkeys(item.provider for item in attempts))
    failures = tuple({
        "provider": item.provider,
        "query": item.query,
        "status": item.status,
        "message": item.message,
        "duration_seconds": item.duration_seconds,
    } for item in attempts if item.status != "success")
    trace = outcome.trace
    counts = {
        "provider_raw": trace.provider_raw_result_count,
        "collected": trace.collected_result_count,
        "normalized": trace.normalized_result_count,
        "unique": trace.unique_candidate_count,
        "duplicates": trace.duplicate_count,
        "accepted": trace.accepted_candidate_count,
        "rejected": trace.rejected_candidate_count,
        "official": len(official_all),
        "dealer": len(grouped["dealer"]),
        "secondary": len(grouped["secondary"]),
        "documents": len(merged_documents),
    }
    return DiscoveryDebugResult(
        product_name=product_name,
        brand=brand,
        model=model,
        market=market,
        status=status,
        exact_official_found=exact_official,
        official=tuple([*official_pages, *support_pages]),
        dealers=tuple(grouped["dealer"]),
        secondary=tuple(grouped["secondary"]),
        rejected=tuple(grouped["rejected"]),
        runtime_seconds=round(runtime_seconds, 3),
        search_status=outcome.search_status,
        attempted_queries=tuple(outcome.attempted_queries),
        providers=providers,
        provider_failures=failures,
        candidate_counts=counts,
        official_pages=tuple(official_pages),
        support_pages=tuple(support_pages),
        documents=tuple(merged_documents),
        rejected_documents=tuple(rejected_documents[:40]),
        document_fields=documents_by_canonical_field(merged_documents),
        page_inspections=tuple(inspection for _, inspection in inspections),
        discovery_metadata=_metadata_from_inspections(inspections),
        official_paths=_official_paths(outcome),
        trace=_trace_payload(outcome, grouped, merged_documents, rejected_documents),
        page_metadata=tuple(_page_metadata(source, inspection) for source, inspection in inspections),
        sku_rejections=_sku_rejections(model, grouped["rejected"], rejected_documents),
        performance=_performance(outcome, counts, runtime_seconds),
    )


_REGION_PATH = re.compile(r"/([a-z]{2})[-_]([a-z]{2})(?:/|$)", re.I)
_LANG_PATH = re.compile(r"^/([a-z]{2})(?:/|$)", re.I)


def locale_region(url: str) -> str:
    """Best-effort locale/region label from the URL alone (path locale, else host TLD)."""
    parsed = urlparse(url)
    match = _REGION_PATH.search(parsed.path)
    if match:
        return f"{match.group(1).lower()}-{match.group(2).upper()}"
    labels = (parsed.hostname or "").lower().split(".")
    if len(labels) >= 2:
        tld = labels[-1]
        if len(tld) == 2:
            return f".{tld}" + (f" ({_LANG_PATH.match(parsed.path).group(1).lower()})" if _LANG_PATH.match(parsed.path) else "")
        if len(labels) >= 3 and labels[-2] in {"global", "eu", "cee", "asia"}:
            return labels[-2]
        return "global (.%s)" % tld
    return "unknown"


def _page_metadata(source: DiscoverySource, inspection: PageInspection) -> dict[str, object]:
    return {
        "url": inspection.url or source.url,
        "domain": source.domain,
        "locale_region": locale_region(inspection.url or source.url),
        "model_match": source.model_match,
        "expandable_specs": inspection.has_expandable_specs,
        "hidden_spec_content": inspection.has_hidden_spec_content,
        "interaction_required": inspection.requires_interaction,
        "spec_location": inspection.spec_location,
        "spec_controls": list(inspection.spec_controls),
        "signals": list(inspection.signals),
        "spec_rows_visible": inspection.spec_rows_visible,
        "spec_rows_hidden": inspection.spec_rows_hidden,
        "json_spec_blocks": inspection.json_spec_blocks,
    }


_ID_TOKEN = re.compile(r"[A-Za-z0-9]+(?:[-_/][A-Za-z0-9]+)*")


def _neighbour_sku(base: str, text: str) -> str:
    """An identifier in ``text`` that looks like a sibling of ``base`` (same shape, not equal)."""
    wanted = base.upper()
    for token in _ID_TOKEN.findall(text):
        compact = re.sub(r"[^A-Za-z0-9]", "", token).upper()
        if compact == wanted or len(compact) < 5 or abs(len(compact) - len(wanted)) > 2:
            continue
        if not (re.search(r"[A-Z]", compact) and re.search(r"\d", compact)):
            continue
        if SequenceMatcher(None, compact, wanted).ratio() >= 0.75:
            return token
    return ""


def _sku_rejections(
    model: str,
    rejected: list[DiscoverySource],
    rejected_documents: list[RejectedDocument],
) -> tuple[dict[str, str], ...]:
    """Near-miss candidates rejected because they carry a different SKU/variant."""
    sku = requested_sku(model)
    if sku is None:
        return ()
    requested = f"{sku.base_display}/{sku.suffix}" if sku.suffix else sku.base_display
    found: list[dict[str, str]] = []
    seen: set[str] = set()
    items = [(item.url, item.title, item.reason) for item in rejected]
    items += [(doc.url, doc.title, doc.reason) for doc in rejected_documents]
    for url, title, reason in items:
        if url in seen:
            continue
        relation = sku_relation(model, url, title)
        if relation.kind in {"different_suffix", "different_variant", "base_only"}:
            seen.add(url)
            found.append({
                "url": url, "found_sku": relation.evidence, "requested_sku": requested,
                "relation": relation.kind, "reason": reason,
            })
            continue
        neighbour = _neighbour_sku(sku.base, f"{unquote(url)} {title}")
        if neighbour and relation.kind == "absent":
            seen.add(url)
            found.append({
                "url": url, "found_sku": neighbour, "requested_sku": requested,
                "relation": "neighbour_sku", "reason": reason,
            })
    return tuple(found[:30])


def _performance(outcome: DiscoveryOutcome, counts: dict[str, int], runtime: float) -> dict[str, object]:
    statuses: dict[str, int] = {}
    for attempt in outcome.provider_attempts:
        statuses[attempt.status] = statuses.get(attempt.status, 0) + 1
    methods: dict[str, int] = {}
    for attempt in outcome.provider_attempts:
        if attempt.provider == "direct_domain_probe":
            for method, count in attempt.method_requests:
                methods[method] = max(methods.get(method, 0), int(count))
    first_path = next(
        (f"{attempt.provider}:{attempt.discovery_method}"
         for attempt in outcome.provider_attempts if attempt.discovery_method),
        "",
    )
    return {
        "first_official_path": first_path,
        "probe_requests": sum(methods.values()),
        "domain_probes": methods.get("domain_resolution", 0),
        "probe_methods": methods,
        "runtime_seconds": round(runtime, 1),
        "slow": "over_60s" if runtime > 60 else "over_30s" if runtime > 30 else "",
        "query_count": len(outcome.attempted_queries),
        "provider_attempts": len(outcome.provider_attempts),
        "raw_candidates": counts.get("provider_raw", 0),
        "unique_candidates": counts.get("unique", 0),
        "accepted": counts.get("accepted", 0),
        "rejected": counts.get("rejected", 0),
        "blocked": statuses.get("blocked", 0),
        "timeout": statuses.get("timeout", 0),
        "circuit_open": statuses.get("circuit_open", 0),
        "attempt_statuses": statuses,
    }


def _official_paths(outcome: DiscoveryOutcome) -> tuple[dict[str, object], ...]:
    """Which generic official-discovery paths ran, and what each produced."""
    paths: dict[str, dict[str, object]] = {}
    for attempt in outcome.provider_attempts:
        if attempt.provider not in {"direct_domain_probe", "browser_official_discovery"}:
            continue
        for method, count in attempt.method_requests:
            entry = paths.setdefault(
                f"{attempt.provider}:{method}",
                {"path": f"{attempt.provider}:{method}", "requests": 0, "queries": 0},
            )
            entry["requests"] = max(int(entry["requests"]), int(count))
            entry["queries"] = int(entry["queries"]) + 1
    for path in ("serp_identity_queries", "serp_domain_restricted_queries", "serp_official_bootstrap"):
        paths.setdefault(path, {"path": path, "requests": 0, "queries": 0})
    for query in outcome.attempted_queries:
        key = (
            "serp_domain_restricted_queries" if "site:" in query
            else "serp_official_bootstrap" if "official" in query
            else "serp_identity_queries"
        )
        paths[key]["queries"] = int(paths[key]["queries"]) + 1
    return tuple(paths.values())


def _trace_payload(
    outcome: DiscoveryOutcome,
    grouped: dict[DiscoveryGroup, list[DiscoverySource]],
    documents: list[OfficialDocument],
    rejected_documents: list[RejectedDocument],
) -> dict[str, object]:
    trace = outcome.trace
    return {
        "queries": list(outcome.attempted_queries),
        "provider_attempts": [asdict(item) for item in outcome.provider_attempts],
        "raw_and_normalized": [asdict(entry) for entry in trace.entries],
        "accepted": [item.to_dict() for group in ("official", "dealer", "secondary") for item in grouped[group]],  # type: ignore[index]
        "rejected": [item.to_dict() for item in grouped["rejected"]],
        "documents": [item.to_dict() for item in documents],
        "rejected_documents": [item.to_dict() for item in rejected_documents],
        "provider_errors": [
            {"provider": item.provider, "query": item.query, "status": item.status, "message": item.message}
            for item in outcome.provider_attempts if item.status in {"blocked", "timeout", "error", "parse_error"}
        ],
    }


def document_queries(brand: str, model: str, domains: list[str]) -> list[str]:
    """Document-focused queries, restricted to verified official domains."""
    sku = requested_sku(model)
    key = sku.base_display if sku is not None else model
    queries: list[str] = []
    for domain in domains[:2]:
        queries.append(f'"{key}" manual site:{domain}')
    if not domains:
        queries.append(f'{brand} "{key}" user manual pdf')
    queries.append(f'{brand} "{key}" declaration of conformity')
    return queries


class DiscoveryDebugService:
    """Synchronous discovery service shared by Telegram and diagnostics."""

    def __init__(
        self, *, wall_clock_budget_seconds: float = 75.0,
        providers: Callable[[], Iterable[object]] | None = None,
        document_reader: DocumentReader | None = None,
    ) -> None:
        if wall_clock_budget_seconds <= 0:
            raise ValueError("wall_clock_budget_seconds must be positive")
        self.wall_clock_budget_seconds = float(wall_clock_budget_seconds)
        # Diagnostics only: restrict the provider chain (e.g. official-only, no SERP).
        self._providers = providers
        self._document_reader = document_reader or read_pdf_text
        self._last_by_chat: dict[int, DiscoveryDebugResult] = {}
        self._lock = threading.Lock()

    def discover_name(
        self, product_name: str, *, market: str = "global", chat_id: int | None = None,
        product_category: str = "unknown",
    ) -> DiscoveryDebugResult:
        identity = parse_discovery_product_name(product_name)
        if identity is None:
            raise ValueError("brand and model are required")
        brand, model = identity
        name = " ".join(product_name.split())
        started = time.monotonic()
        budget = WallClockBudget(self.wall_clock_budget_seconds)
        runtime = DiscoveryRuntimeConfig()
        page_cache: dict[str, tuple[str, str] | None] = {}
        page_fetches: list[dict[str, object]] = []
        document_cache: dict[str, object] = {}

        def fetch(url: str) -> tuple[str, str] | None:
            if url not in page_cache:
                remaining = budget.remaining_seconds
                if remaining < 8.0:
                    page_cache[url] = None
                    page_fetches.append({"url": url, "status": "budget_exhausted", "duration_seconds": 0.0})
                else:
                    fetch_started = time.monotonic()
                    page_cache[url] = fetch_working_page(url, timeout=min(12.0, remaining))
                    page_fetches.append({
                        "url": url,
                        "status": "loaded" if page_cache[url] else "unavailable",
                        "duration_seconds": round(time.monotonic() - fetch_started, 3),
                        "final_url": page_cache[url][0] if page_cache[url] else "",
                    })
            return page_cache[url]

        def document_reader(url: str):
            if url not in document_cache:
                document_cache[url] = self._document_reader(url) if budget.remaining_seconds >= 5.0 else None
            return document_cache[url]

        provider_chain = list(self._providers()) if self._providers is not None else None
        with ResilientSearchSession(
            market, provider_chain, config=runtime, budget=budget,
        ) as session:
            outcome = discover_with_status(
                brand, model, market=market, searcher=session.search_with_status,
                early_official_stop=True, product_category=product_category,
            )
            result = _result_from_outcome(
                name, brand, model, market, outcome, time.monotonic() - started, fetch=fetch,
                document_reader=document_reader, product_category=product_category,
            )
            # Documents linked from the official pages are the cheap, reliable
            # source.  Search-engine document queries only run when they did
            # not already produce a manual-type document.
            verified_hosts = list(dict.fromkeys([
                *_official_hosts(outcome.candidates),
                *(page.domain for page in result.official_pages if page.authority_status == "verified"),
            ]))
            if verified_hosts and not any(doc.doc_type in _MANUAL_TYPES for doc in result.documents):
                extra: list[object] = []
                attempted = list(outcome.attempted_queries)
                attempts = list(outcome.provider_attempts)
                for query in document_queries(brand, model, verified_hosts):
                    if budget.remaining_seconds < 8:
                        break
                    attempted.append(query)
                    try:
                        response = session.search_with_status(
                            query, skip_providers=_OFFICIAL_SURFACE_PROVIDERS,
                        )
                    except Exception:  # noqa: BLE001 - a failed query is reported via attempts
                        continue
                    attempts.extend(response.attempts)
                    extra.extend(response.results)
                if extra or len(attempted) != len(outcome.attempted_queries):
                    outcome = DiscoveryOutcome(
                        candidates=outcome.candidates, search_status=outcome.search_status,
                        queries=outcome.queries, attempted_queries=attempted, issues=outcome.issues,
                        provider_attempts=attempts, rejected_candidates=outcome.rejected_candidates,
                        trace=outcome.trace,
                    )
                    result = _result_from_outcome(
                        name, brand, model, market, outcome, time.monotonic() - started,
                        fetch=fetch, extra_results=[_search_result_record(item) for item in extra],
                        document_reader=document_reader, product_category=product_category,
                    )
        elapsed = round(time.monotonic() - started, 3)
        result = replace(result, runtime_seconds=elapsed, page_fetches=tuple(page_fetches),
                         performance={**result.performance, "runtime_seconds": elapsed,
                                      "budget_overrun_seconds": round(max(0.0, elapsed - self.wall_clock_budget_seconds), 3),
                                      "timeout_can_interrupt_active_requests": False})
        if chat_id is not None:
            with self._lock:
                self._last_by_chat[chat_id] = result
        return result

    def last_result(self, chat_id: int) -> DiscoveryDebugResult | None:
        with self._lock:
            return self._last_by_chat.get(chat_id)
