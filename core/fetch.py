"""Structured transport for HTML, text, and PDF discovery candidates.

Fetching and product validation are intentionally separate. In particular, a
successful official-domain fetch does not prove SKU relevance, and a PDF that
mentions many models must not later be treated as an exact match for all of
them. GTIN/EAN/UPC remains an optional strong identity signal.
"""

from __future__ import annotations

from io import BytesIO
import os
import re
from typing import Literal, TypedDict
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup


FetchStatus = Literal["success", "blocked", "not_found", "unsupported", "error"]
FetchMethod = Literal["requests", "playwright", "pdf"]
DocumentType = Literal["pdf", "html", "text", "binary", "unknown"]
TextStatus = Literal["available", "text_not_available", "not_applicable"]


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
        )),
    )
    for reason, markers in patterns:
        if any(marker in text for marker in markers):
            return reason
    return None


def _html_needs_browser(html: str, text: str) -> bool:
    low = (html or "").lower()
    return len(text.strip()) < 80 or (
        len(text.strip()) < 500
        and any(marker in low for marker in ("enable javascript", "javascript is required", "id=\"root\"", "id=\"app\""))
    )


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


def _status_result(source_url: str, final_url: str, http_status: int,
                   content_type: str, method: FetchMethod = "requests") -> FetchResult | None:
    if http_status == 404:
        return _result(source_url, final_url=final_url, status="not_found",
                       http_status=http_status, fetch_method=method, content_type=content_type)
    if http_status in {401, 403}:
        reason = "login_required" if http_status == 401 else "access_denied"
        return _result(source_url, final_url=final_url, status="blocked",
                       http_status=http_status, fetch_method=method, content_type=content_type,
                       blocked_reason=reason)
    if http_status >= 500:
        return _result(source_url, final_url=final_url, status="error",
                       http_status=http_status, fetch_method=method, content_type=content_type,
                       error=f"Source returned HTTP {http_status}")
    if http_status >= 400:
        return _result(source_url, final_url=final_url, status="error",
                       http_status=http_status, fetch_method=method, content_type=content_type,
                       error=f"Source returned HTTP {http_status}")
    return None


def fetch_source(url: str, *, timeout: float = 30, max_bytes: int = 25_000_000,
                 session: requests.Session | None = None) -> FetchResult:
    """Fetch a source requests-first, rendering only insufficient unblocked HTML."""
    source_url = (url or "").strip()
    parsed = urlparse(source_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return _result(source_url, status="unsupported", document_type="unknown",
                       error="Only absolute HTTP(S) URLs are supported")
    if max_bytes <= 0:
        return _result(source_url, status="error", error="max_bytes must be positive")

    client = session or requests.Session()
    try:
        response = client.get(
            source_url,
            timeout=timeout,
            allow_redirects=True,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36"
                ),
                "Accept": "application/pdf,text/html,text/plain;q=0.9,*/*;q=0.5",
            },
        )
    except requests.RequestException as error:
        return _result(source_url, status="error", error=f"Request failed: {error}")

    final_url = response.url
    http_status = response.status_code
    content_type = response.headers.get("Content-Type", "")
    status_result = _status_result(source_url, final_url, http_status, content_type)
    if status_result:
        return status_result
    content = response.content
    if len(content) > max_bytes:
        return _result(source_url, final_url=final_url, status="error", http_status=http_status,
                       content_type=content_type, error=f"Source exceeds {max_bytes}-byte limit")
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

    try:
        rendered_url, rendered_status, rendered_html = _fetch_with_playwright(source_url, timeout)
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
