"""Structured transport for HTML, text, and PDF discovery candidates.

Fetching and product validation are intentionally separate. In particular, a
successful official-domain fetch does not prove SKU relevance, and a PDF that
mentions many models must not later be treated as an exact match for all of
them. GTIN/EAN/UPC remains an optional strong identity signal.
"""

from __future__ import annotations

from io import BytesIO
import json
import os
import re
import time
from typing import Literal, Mapping, TypedDict
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from core.provider_health import FailureClass, HEALTH_AFFECTING_FAILURE_CLASSES, ProviderHealthStore


FetchStatus = Literal["success", "blocked", "not_found", "unsupported", "error"]
FetchMethod = Literal["requests", "playwright", "pdf"]
DocumentType = Literal["pdf", "html", "text", "binary", "unknown"]
TextStatus = Literal["available", "text_not_available", "not_applicable"]
MAX_BROWSER_FALLBACK_SECONDS = 5.0
MIN_BROWSER_FALLBACK_SECONDS = 1.0
FETCH_RETRY_BACKOFF_SECONDS = 0.3
MIN_RETRY_REMAINING_SECONDS = 1.0
# Failure classes worth one bounded retry before giving up on a fetch. A
# connection reset/refused or a bare request timeout is plausibly transient;
# a 429 clears quickly often enough to be worth one retry with backoff.
# WAF/403 (BLOCKED) and malformed content are deliberately excluded -- the
# same page will answer the same way immediately.
_FETCH_RETRYABLE_CLASSES = frozenset({
    FailureClass.TIMEOUT, FailureClass.CONNECTION_ERROR, FailureClass.RATE_LIMITED,
})


class FetchResult(TypedDict):
    source_url: str
    final_url: str
    status: FetchStatus
    http_status: int | None
    fetch_method: FetchMethod
    content_type: str
    document_type: DocumentType
    html: str
    text: str
    content: bytes
    pdf_text: str
    text_status: TextStatus
    blocked_reason: str | None
    error: str | None
    failure_class: str | None
    retried: bool
    duration_seconds: float
    source_type: str | None
    authority_status: str | None
    authority_evidence_url: str | None
    authority_role: str | None
    model_relevance: str | None
    identity_relation: str | None
    discovery_metadata: dict[str, object]


def _result(source_url: str, **updates: object) -> FetchResult:
    result: FetchResult = {
        "source_url": source_url,
        "final_url": source_url,
        "status": "error",
        "http_status": None,
        "fetch_method": "requests",
        "content_type": "",
        "document_type": "unknown",
        "html": "",
        "text": "",
        "content": b"",
        "pdf_text": "",
        "text_status": "not_applicable",
        "blocked_reason": None,
        "error": None,
        "failure_class": None,
        "retried": False,
        "duration_seconds": 0.0,
        "source_type": None,
        "authority_status": None,
        "authority_evidence_url": None,
        "authority_role": None,
        "model_relevance": None,
        "identity_relation": None,
        "discovery_metadata": {},
    }
    result.update(updates)  # type: ignore[typeddict-item]
    return result


def _document_type(content_type: str, final_url: str, content: bytes) -> DocumentType:
    media_type = content_type.partition(";")[0].strip().lower()
    path = urlparse(final_url).path.lower()
    if media_type == "application/pdf" or path.endswith(".pdf") or content.startswith(b"%PDF"):
        return "pdf"
    if media_type in {"text/html", "application/xhtml+xml"}:
        return "html"
    if media_type.startswith("text/"):
        return "text"
    if media_type.startswith(("image/", "video/")) or media_type in {
        "application/octet-stream", "application/zip",
    }:
        return "binary"
    return "unknown"


def _extract_pdf_text(content: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as error:
        raise RuntimeError(
            "PDF text extraction requires pypdf; run 'pip install -r requirements.txt'."
        ) from error
    reader = PdfReader(BytesIO(content))
    pages = []
    for page in reader.pages:
        value = page.extract_text() or ""
        if value.strip():
            pages.append(value.strip())
    return "\n\n".join(pages).strip()


def _decode_response(response: requests.Response) -> str:
    if not response.encoding:
        response.encoding = response.apparent_encoding or "utf-8"
    return response.text


def _extract_html_text(html: str) -> str:
    soup = BeautifulSoup(html or "", "html.parser")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    description_node = soup.find("meta", attrs={"name": re.compile(r"^description$", re.I)})
    description = str(description_node.get("content", "")).strip() if description_node else ""
    for node in soup.select("script, style, noscript, svg"):
        node.decompose()
    body = soup.body or soup
    body_text = body.get_text(" ", strip=True)
    values = []
    for value in (title, description, body_text):
        normalized = re.sub(r"\s+", " ", value).strip()
        if normalized and normalized not in values:
            values.append(normalized)
    return "\n".join(values)


def _blocked_reason(value: str) -> str | None:
    text = (value or "").lower()
    patterns = (
        ("captcha", ("captcha", "recaptcha")),
        ("access_denied", ("access denied", "access is denied", "403 forbidden")),
        ("login_required", ("login required", "sign in to continue", "log in to continue")),
        ("paywall", ("paywall", "subscribe to continue", "subscribers only")),
        ("bot_challenge", (
            "verify you are human", "unusual traffic", "checking your browser",
            "cloudflare ray id", "attention required", "just a moment",
            "qrator", "__qrator__", "/__qrator/",
        )),
    )
    for reason, markers in patterns:
        if any(marker in text for marker in markers):
            return reason
    return None


def _html_needs_browser(html: str, text: str) -> bool:
    return not _html_content_complete(html, text)


def _tag_signals(node: object) -> str:
    if not hasattr(node, "get"):
        return ""
    node_id = str(node.get("id") or "")  # type: ignore[attr-defined]
    classes = node.get("class") or []  # type: ignore[attr-defined]
    if isinstance(classes, str):
        classes = [classes]
    # Utility-CSS frameworks (Tailwind and equivalents) encode conditional
    # styling as "<modifier[-modifier...]>:<utility>" class tokens, e.g.
    # "peer-placeholder-shown:translate-y-0" or "hover:opacity-100". A
    # marker word appearing only inside such a token (observed live:
    # "placeholder" inside a completely unrelated input's variant classes)
    # describes a CSS state, not page-content completeness, so these tokens
    # are excluded before signal matching.
    semantic_classes = [str(value) for value in classes if ":" not in str(value)]
    return " ".join([node_id, *semantic_classes]).casefold()


def _meaningful_product_json_ld(soup: BeautifulSoup) -> bool:
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
            if any(str(value).casefold() == "product" for value in node_types) and any(
                key in node for key in (
                    "additionalProperty", "mpn", "gtin", "gtin8", "gtin12",
                    "gtin13", "gtin14", "weight", "width", "height", "depth",
                )
            ):
                return True
            pending.extend(node.values())
    return False


def _html_content_complete(html: str, text: str) -> bool:
    """Conservatively detect HTML shells whose specification values still need JS."""
    low = (html or "").lower()
    soup = BeautifulSoup(html or "", "html.parser")
    if _meaningful_product_json_ld(soup):
        return True
    if len(text.strip()) < 80 or (
        len(text.strip()) < 500
        and any(marker in low for marker in ("enable javascript", "javascript is required", "id=\"root\"", "id=\"app\""))
    ):
        return False

    # \b before each alternative avoids matching "spec" inside an unrelated
    # word like "aspect" (observed live: image-gallery thumbnails classed
    # "aspect-square" were misread as specification containers).
    spec_marker = re.compile(r"\b(?:spec|attribute|characteristic|parameter|product[-_ ]?detail)", re.I)
    label_marker = re.compile(r"label|name|key|title|heading", re.I)
    value_marker = re.compile(r"value|description|desc|content|data", re.I)
    skeleton_marker = re.compile(r"\b(?:skeleton|placeholder|loading|shimmer)\b", re.I)

    spec_nodes = [node for node in soup.find_all(True) if spec_marker.search(_tag_signals(node))]
    if not spec_nodes:
        return True

    pending_values = 0
    empty_values = 0
    labels = 0
    skeletons = 0
    for node in soup.find_all(True):
        signals = _tag_signals(node)
        if skeleton_marker.search(signals):
            skeletons += 1
        inside_specs = spec_marker.search(signals) or any(
            spec_marker.search(_tag_signals(parent)) for parent in node.parents
        )
        if not inside_specs:
            continue
        visible = node.get_text(" ", strip=True)
        if (label_marker.search(signals) or node.name in {"h2", "h3", "h4", "dt"}) and visible:
            labels += 1
        if value_marker.search(signals) and not visible:
            empty_values += 1
            if str(node.get("data-value") or "").strip():
                pending_values += 1

    if skeletons >= 2:
        return False
    if labels >= 3 and (
        pending_values >= 3 or (len(spec_nodes) >= 5 and empty_values >= 3)
    ):
        return False
    return True


def _browser_headless() -> bool:
    return os.getenv("PDV_BROWSER_HEADLESS", "true").strip().lower() not in {"0", "false", "no", "off"}


def _fetch_with_playwright(url: str, timeout: float) -> tuple[str, int | None, str]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as error:
        raise RuntimeError("Playwright is required to render this HTML source.") from error
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=_browser_headless())
        try:
            page = browser.new_page()
            response = page.goto(url, wait_until="networkidle", timeout=int(timeout * 1000))
            return page.url, response.status if response else None, page.content()
        finally:
            browser.close()


def _not_found_result(source_url: str, final_url: str, http_status: int,
                      content_type: str, method: FetchMethod = "requests") -> FetchResult:
    return _result(source_url, final_url=final_url, status="not_found",
                   http_status=http_status, fetch_method=method, content_type=content_type)


def _error_status_result(source_url: str, final_url: str, http_status: int,
                         content_type: str, method: FetchMethod = "requests") -> FetchResult:
    return _result(source_url, final_url=final_url, status="error",
                   http_status=http_status, fetch_method=method, content_type=content_type,
                   error=f"Source returned HTTP {http_status}")


def _access_denied_result(source_url: str, final_url: str, http_status: int,
                          content_type: str, content: bytes,
                          method: FetchMethod = "requests") -> FetchResult:
    """Classify a 401/403 response from actual body evidence, not status code alone.

    A WAF/bot-mitigation challenge (Qrator, Cloudflare, etc.) commonly answers
    with 401/403 even though the page is not an authentication wall. Reusing
    the same evidence markers as a normal blocked page keeps the reason
    truthful instead of asserting "login_required" whenever a site happens to
    answer with HTTP 401.
    """
    default_reason = "login_required" if http_status == 401 else "access_denied"
    document_type = _document_type(content_type, final_url, content)
    if document_type != "html":
        return _result(source_url, final_url=final_url, status="blocked",
                       http_status=http_status, fetch_method=method, content_type=content_type,
                       document_type=document_type, content=content, blocked_reason=default_reason)
    html = content.decode("utf-8", errors="replace")
    text = _extract_html_text(html)
    reason = _blocked_reason(f"{html[:200_000]} {text[:20_000]}") or default_reason
    return _result(source_url, final_url=final_url, status="blocked",
                   http_status=http_status, fetch_method=method, content_type=content_type,
                   document_type="html", html=html, content=content, text=text,
                   text_status="available" if text else "text_not_available",
                   blocked_reason=reason)


def _classify_request_exception(error: Exception) -> str:
    if isinstance(error, requests.exceptions.Timeout):
        return FailureClass.TIMEOUT
    if isinstance(error, requests.exceptions.ConnectionError):
        return FailureClass.CONNECTION_ERROR
    return FailureClass.OTHER


def _get_with_retry(
    client: requests.Session,
    url: str,
    *,
    timeout: float,
    headers: Mapping[str, str],
    started: float,
) -> tuple[requests.Response | None, Exception | None, str | None, bool]:
    """GET with one bounded retry for a transient failure class.

    Returns ``(response, error, failure_class, retried)``. A connection
    error/timeout that fails fast, or a 429, gets one retry with a short
    backoff if there is still time left in ``timeout``; a WAF/403-style
    response is not an exception here at all (handled by the caller from the
    status code) so it is never retried by this function.
    """
    error: Exception | None = None
    failure_class: str | None = None
    retried = False
    for attempt in (0, 1):
        remaining = timeout - (time.monotonic() - started)
        if attempt == 1 and remaining < MIN_RETRY_REMAINING_SECONDS:
            break
        request_timeout = timeout if attempt == 0 else max(MIN_RETRY_REMAINING_SECONDS, remaining)
        try:
            response = client.get(
                url, timeout=request_timeout, allow_redirects=True, headers=headers,
            )
        except requests.RequestException as exc:
            error = exc
            failure_class = _classify_request_exception(exc)
            if attempt == 0 and failure_class in _FETCH_RETRYABLE_CLASSES:
                retried = True
                time.sleep(FETCH_RETRY_BACKOFF_SECONDS)
                continue
            return None, error, failure_class, retried
        if response.status_code == 429 and attempt == 0:
            retried = True
            time.sleep(FETCH_RETRY_BACKOFF_SECONDS)
            continue
        return response, None, None, retried
    return None, error, failure_class, retried


def fetch_source(url: str, *, timeout: float = 30, max_bytes: int = 25_000_000,
                 session: requests.Session | None = None) -> FetchResult:
    """Fetch a source requests-first, rendering only insufficient unblocked HTML."""
    started = time.monotonic()
    result = _fetch_source_impl(url, timeout=timeout, max_bytes=max_bytes, session=session, started=started)
    result["duration_seconds"] = round(max(0.0, time.monotonic() - started), 6)
    return result


def _fetch_source_impl(
    url: str, *, timeout: float, max_bytes: int,
    session: requests.Session | None, started: float,
) -> FetchResult:
    source_url = (url or "").strip()
    parsed = urlparse(source_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return _result(source_url, status="unsupported", document_type="unknown",
                       error="Only absolute HTTP(S) URLs are supported")
    if max_bytes <= 0:
        return _result(source_url, status="error", error="max_bytes must be positive")

    client = session or requests.Session()
    response, error, failure_class, retried = _get_with_retry(
        client, source_url, timeout=timeout, started=started,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36"
            ),
            "Accept": "application/pdf,text/html,text/plain;q=0.9,*/*;q=0.5",
        },
    )
    if response is None:
        return _result(
            source_url, status="error", error=f"Request failed: {error}",
            failure_class=failure_class or FailureClass.OTHER, retried=retried,
        )

    final_url = response.url
    http_status = response.status_code
    content_type = response.headers.get("Content-Type", "")
    if http_status == 404:
        return _not_found_result(source_url, final_url, http_status, content_type)
    content = response.content
    if len(content) > max_bytes:
        return _result(source_url, final_url=final_url, status="error", http_status=http_status,
                       content_type=content_type, error=f"Source exceeds {max_bytes}-byte limit",
                       failure_class=FailureClass.OTHER, retried=retried)
    if http_status in {401, 403}:
        result = _access_denied_result(source_url, final_url, http_status, content_type, content)
        result["failure_class"] = FailureClass.BLOCKED
        result["retried"] = retried
        return result
    if http_status == 429:
        result = _error_status_result(source_url, final_url, http_status, content_type)
        result["failure_class"] = FailureClass.RATE_LIMITED
        result["retried"] = retried
        return result
    if http_status >= 500:
        result = _error_status_result(source_url, final_url, http_status, content_type)
        result["failure_class"] = FailureClass.UNAVAILABLE
        result["retried"] = retried
        return result
    if http_status >= 400:
        result = _error_status_result(source_url, final_url, http_status, content_type)
        result["failure_class"] = FailureClass.OTHER
        result["retried"] = retried
        return result
    document_type = _document_type(content_type, final_url, content)

    if document_type == "pdf":
        try:
            pdf_text = _extract_pdf_text(content)
            extraction_error = None
        except RuntimeError:
            raise
        except Exception as error:
            pdf_text = ""
            extraction_error = f"PDF text extraction failed: {error}"
        return _result(
            source_url, final_url=final_url, status="success", http_status=http_status,
            fetch_method="pdf", content_type=content_type, document_type="pdf",
            content=content, text=pdf_text, pdf_text=pdf_text,
            text_status="available" if pdf_text else "text_not_available",
            error=extraction_error,
        )

    if document_type == "binary" or document_type == "unknown":
        return _result(source_url, final_url=final_url, status="unsupported",
                       http_status=http_status, content_type=content_type,
                       document_type=document_type, content=content)

    decoded = _decode_response(response)
    if document_type == "text":
        text = re.sub(r"\s+", " ", decoded).strip()
        return _result(source_url, final_url=final_url, status="success",
                       http_status=http_status, content_type=content_type,
                       document_type="text", content=content, text=text,
                       text_status="available" if text else "text_not_available")

    html = decoded
    text = _extract_html_text(html)
    reason = _blocked_reason(f"{html[:200_000]} {text[:20_000]}")
    if reason:
        return _result(source_url, final_url=final_url, status="blocked",
                       http_status=http_status, content_type=content_type,
                       document_type="html", html=html, content=content, text=text,
                       text_status="available" if text else "text_not_available",
                       blocked_reason=reason)
    if not _html_needs_browser(html, text):
        return _result(source_url, final_url=final_url, status="success",
                       http_status=http_status, content_type=content_type,
                       document_type="html", html=html, content=content, text=text,
                       text_status="available")

    remaining = timeout - (time.monotonic() - started)
    if remaining < MIN_BROWSER_FALLBACK_SECONDS:
        return _result(
            source_url, final_url=final_url,
            status="success" if text else "error", http_status=http_status,
            content_type=content_type, document_type="html", html=html,
            content=content, text=text,
            text_status="available" if text else "text_not_available",
            error=None if text else "Insufficient fetch deadline for browser fallback.",
        )
    browser_timeout = min(MAX_BROWSER_FALLBACK_SECONDS, remaining)
    try:
        rendered_url, rendered_status, rendered_html = _fetch_with_playwright(
            source_url, browser_timeout,
        )
    except Exception as error:
        return _result(source_url, final_url=final_url, status="error", http_status=http_status,
                       content_type=content_type, document_type="html", html=html,
                       content=content, text=text,
                       text_status="available" if text else "text_not_available",
                       error=f"Playwright fallback failed: {error}")
    rendered_text = _extract_html_text(rendered_html)
    rendered_reason = _blocked_reason(f"{rendered_html[:200_000]} {rendered_text[:20_000]}")
    if rendered_reason:
        return _result(source_url, final_url=rendered_url, status="blocked",
                       http_status=rendered_status, fetch_method="playwright",
                       content_type="text/html", document_type="html",
                       html=rendered_html, content=rendered_html.encode("utf-8"),
                       text=rendered_text,
                       text_status="available" if rendered_text else "text_not_available",
                       blocked_reason=rendered_reason)
    if not rendered_text:
        return _result(source_url, final_url=rendered_url, status="error",
                       http_status=rendered_status, fetch_method="playwright",
                       content_type="text/html", document_type="html", html=rendered_html,
                       content=rendered_html.encode("utf-8"), text_status="text_not_available",
                       error="Rendered page contains no meaningful text")
    return _result(source_url, final_url=rendered_url, status="success",
                   http_status=rendered_status, fetch_method="playwright",
                   content_type="text/html", document_type="html", html=rendered_html,
                   content=rendered_html.encode("utf-8"), text=rendered_text,
                   text_status="available")


def _retry_verified_candidate_with_browser(
    result: FetchResult,
    candidate: Mapping[str, object],
    timeout: float,
) -> FetchResult:
    """Retry a blocked/failed, discovery-verified first-party page in a browser.

    Retailers and unknown domains deliberately remain requests-only here. The
    retry changes transport, never authority, and rendered challenge pages
    remain structured as blocked.
    """
    if result.get("status") not in {"blocked", "error"}:
        return result
    if candidate.get("authority_status") != "verified":
        return result
    if candidate.get("source_type") not in {"manufacturer", "official_document"}:
        return result

    source_url = str(candidate.get("url") or result.get("source_url") or "")
    try:
        rendered_url, rendered_status, rendered_html = _fetch_with_playwright(
            source_url, timeout,
        )
    except Exception:
        return result

    rendered_text = _extract_html_text(rendered_html)
    rendered_reason = _blocked_reason(
        f"{rendered_html[:200_000]} {rendered_text[:20_000]}"
    )
    if rendered_reason:
        return _result(
            source_url,
            final_url=rendered_url,
            status="blocked",
            http_status=rendered_status,
            fetch_method="playwright",
            content_type="text/html",
            document_type="html",
            html=rendered_html,
            content=rendered_html.encode("utf-8"),
            text=rendered_text,
            text_status="available" if rendered_text else "text_not_available",
            blocked_reason=rendered_reason,
        )
    if not rendered_text:
        return result
    return _result(
        source_url,
        final_url=rendered_url,
        status="success",
        http_status=rendered_status,
        fetch_method="playwright",
        content_type="text/html",
        document_type="html",
        html=rendered_html,
        content=rendered_html.encode("utf-8"),
        text=rendered_text,
        text_status="available",
    )


def fetch_candidate(
    candidate: Mapping[str, object],
    *,
    timeout: float = 30,
    max_bytes: int = 25_000_000,
    session: requests.Session | None = None,
    health_store: ProviderHealthStore | None = None,
) -> FetchResult:
    """Fetch one Discovery candidate and preserve its explicit source metadata.

    Like ``ResilientSearchSession``, this consults an opt-in, cross-process
    ``ProviderHealthStore`` keyed by host: a host with repeated recent
    timeouts/connection errors/WAF blocks/5xx elsewhere in the same run is
    skipped without a network call, so one already-known-dead host cannot
    keep costing every product in the run its own full fetch timeout.
    """
    store = health_store if health_store is not None else ProviderHealthStore.from_env()
    started = time.monotonic()
    url = str(candidate.get("url") or "")
    host = urlparse(url).hostname or ""
    shared_key = f"fetch:{host}" if host else None
    if shared_key and store.is_open(shared_key):
        result = _result(
            url, status="error",
            error=(
                f"Shared fetch circuit open for {host}: repeated failures "
                "observed elsewhere in this run; skipping without a request."
            ),
            failure_class=FailureClass.UNAVAILABLE,
        )
    else:
        result = fetch_source(url, timeout=timeout, max_bytes=max_bytes, session=session)
        if shared_key:
            if result.get("status") == "success":
                store.record_success(shared_key)
            else:
                failure_class = result.get("failure_class")
                if failure_class in HEALTH_AFFECTING_FAILURE_CLASSES:
                    store.record_failure(shared_key, str(failure_class))
        remaining = timeout - (time.monotonic() - started)
        if remaining >= MIN_BROWSER_FALLBACK_SECONDS:
            result = _retry_verified_candidate_with_browser(
                result, candidate, min(MAX_BROWSER_FALLBACK_SECONDS, remaining),
            )
    result["discovery_metadata"] = dict(candidate)
    for field in (
        "source_type",
        "authority_status",
        "authority_evidence_url",
        "model_relevance",
        "identity_relation",
    ):
        value = candidate.get(field)
        result[field] = str(value) if value is not None else None  # type: ignore[literal-required]
    return result
