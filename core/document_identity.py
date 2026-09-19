"""Stage 34.1: does an official document actually identify the requested product?

A link from an official page, an official host, a product-family resemblance or a
neighbouring internal identifier is *context*, not identity.  A document is
``exact`` only when its own text names the requested identifier.  The verdicts:

* ``exact``      -- the text names the requested SKU (or its regional-suffix twin);
* ``probable``   -- family evidence only: the base SKU without a non-regional requested suffix, the
                    model stem (``EC685`` for ``EC685M``) or every product-name word
                    (``Sonicare 9900 Prestige``) but not the SKU;
* ``unverified`` -- nothing in the text identifies the requested model (a different
                    internal identifier, generic text, an unreadable / scanned file).

No brand or product knowledge is involved.
"""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
import re
import time
from typing import Callable, Sequence

from core.match import normalize_model, normalize_text
from core.sku import classify_sku_suffix, requested_sku, sku_relation

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36"
    ),
    "Accept": "*/*",
}
_MAX_BYTES = 25_000_000
_MAX_PAGES = 200
_TIME_CAP_SECONDS = 25.0


@dataclass(frozen=True, slots=True)
class DocumentText:
    pages: tuple[str, ...] = ()
    status: str = "ok"        # ok | fetch_failed | unreadable | no_text_layer
    complete: bool = True     # False when the page/time cap stopped the scan early


@dataclass(frozen=True, slots=True)
class IdentityVerdict:
    match: str                # exact | probable | unverified
    evidence: str             # document_text | family_base | family_stem | family_words | ...
    reason: str


DocumentReader = Callable[[str], "DocumentText"]


def read_pdf_text(url: str, *, attempts: int = 2) -> DocumentText:
    """Download a PDF and extract its text layer page by page (bounded)."""
    import requests

    body = b""
    for attempt in range(attempts):
        try:
            response = requests.get(url, headers=_HEADERS, timeout=30, allow_redirects=True, stream=True)
        except requests.RequestException:
            time.sleep(1.0 + attempt)
            continue
        try:
            if response.status_code >= 400:
                time.sleep(1.0 + attempt)
                continue
            chunks, size = [], 0
            for chunk in response.iter_content(128 * 1024):
                chunks.append(chunk)
                size += len(chunk)
                if size >= _MAX_BYTES:
                    break
            body = b"".join(chunks)
            break
        finally:
            response.close()
    if not body:
        return DocumentText(status="fetch_failed")
    try:
        from pypdf import PdfReader

        reader = PdfReader(BytesIO(body))
        total = len(reader.pages)
        pages: list[str] = []
        started = time.monotonic()
        for index, page in enumerate(reader.pages):
            if index >= _MAX_PAGES or time.monotonic() - started > _TIME_CAP_SECONDS:
                break
            try:
                pages.append(page.extract_text() or "")
            except Exception:  # noqa: BLE001 - one unreadable page must not lose the document
                pages.append("")
    except Exception:  # noqa: BLE001
        return DocumentText(status="unreadable")
    if not any(page.strip() for page in pages):
        return DocumentText(status="no_text_layer")
    return DocumentText(tuple(pages), "ok", complete=len(pages) >= total)


def _model_stem(model: str) -> str:
    compact = re.sub(r"[^A-Za-z0-9]", "", model or "").upper()
    stem = re.sub(r"(?<=\d)[A-Z]$", "", compact)
    return stem if stem != compact and len(stem) >= 5 else ""


def _name_words(model: str, brand: str) -> list[str]:
    """Product-name words of the model string (not the SKU, not the brand)."""
    sku = requested_sku(model)
    skip = {normalize_model(brand)}
    if sku is not None:
        skip |= {sku.base, sku.base + sku.suffix, sku.suffix}
    words = []
    for word in re.findall(r"[^\W_]+", model or ""):
        key = normalize_model(word)
        if len(key) >= 2 and key not in skip:
            words.append(key)
    return words


def verify_document_identity(
    model: str, brand: str, document: DocumentText,
) -> IdentityVerdict:
    """Identity verdict from the document's own text."""
    if document.status != "ok":
        detail = {
            "fetch_failed": "the document could not be downloaded",
            "unreadable": "the document could not be parsed",
            "no_text_layer": "the document has no text layer (scanned/image file)",
        }.get(document.status, document.status)
        return IdentityVerdict("unverified", document.status, f"{detail}: identity cannot be proven")
    text = "\n".join(document.pages)
    relation = sku_relation(model, text)
    if relation.kind == "exact":
        return IdentityVerdict("exact", "document_text", "the document text names the requested identifier")
    if relation.kind == "regional_suffix":
        return IdentityVerdict(
            "exact", "document_text",
            f"the document text names the base SKU with regional/market code '-{relation.suffix}'",
        )
    if relation.kind == "base_only":
        sku = requested_sku(model)
        if sku is not None and sku.suffix and classify_sku_suffix(sku.suffix) == "regional":
            # HX9992 in the text for a requested HX9992/12: '/12' is a market code, the product is named
            return IdentityVerdict(
                "exact", "document_text",
                f"the document text names the base SKU; the requested '/{sku.suffix}' is a regional/market code",
            )
        return IdentityVerdict(
            "probable", "family_base", "the document names the base SKU but not the requested suffix",
        )
    stem = _model_stem(model)
    if stem and any(
        re.sub(r"[^A-Za-z0-9]", "", token).upper() == stem
        for token in re.findall(r"[A-Za-z0-9]+(?:[-_/.][A-Za-z0-9]+)*", text)
    ):
        return IdentityVerdict(
            "probable", "family_stem", f"the document names the model family '{stem}', not the requested SKU",
        )
    words = _name_words(model, brand)
    if len(words) >= 2:
        tokens = {normalize_model(item) for item in re.findall(r"[^\W_]+", normalize_text(text))}
        if all(word in tokens for word in words):
            return IdentityVerdict(
                "probable", "family_words",
                "the document names the product family/name words but not the requested SKU",
            )
    if relation.kind in {"different_suffix", "different_variant"}:
        return IdentityVerdict(
            "unverified", "different_identifier",
            f"the document names a different identifier ({relation.evidence or relation.suffix}), not the requested SKU",
        )
    scope = "in the scanned pages" if not document.complete else "in the text"
    return IdentityVerdict("unverified", "not_in_text", f"the requested identifier does not appear {scope}")


def verify_documents(
    documents: Sequence[object],
    model: str,
    brand: str,
    reader: DocumentReader,
    *,
    only_matches: frozenset[str] = frozenset({"exact"}),
) -> dict[str, IdentityVerdict]:
    """Verdicts for the PDF documents whose current match is in ``only_matches`` (keyed by URL)."""
    from concurrent.futures import ThreadPoolExecutor
    from urllib.parse import urlparse

    targets = [
        doc for doc in documents
        if getattr(doc, "model_match", "") in only_matches
        and urlparse(getattr(doc, "url", "")).path.lower().endswith(".pdf")
    ]
    if not targets:
        return {}
    with ThreadPoolExecutor(max_workers=min(4, len(targets))) as pool:
        texts = list(pool.map(lambda doc: reader(doc.url), targets))
    return {
        doc.url: verify_document_identity(model, brand, text) for doc, text in zip(targets, texts)
    }
