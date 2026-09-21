"""DOM-only inspection of an official product page for hidden specification data.

Stage 33.1 does not extract specifications.  It only records *how* a page keeps
them, so a later extractor knows whether a browser click is needed:

* ``has_expandable_specs``   -- a "show all specs" style control / tab / accordion;
* ``has_hidden_spec_content``-- spec-like content sits in the HTML but is collapsed
  (``hidden``, ``display:none``, closed ``<details>``, inactive tab, template, JSON state);
* ``requires_interaction``   -- the spec content is *not* in the delivered HTML.

The HTML is analysed without clicking anything.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from html.parser import HTMLParser
import json
import re
import time
from typing import Literal

Tri = Literal["true", "false", "unknown"]

_CONTROL_TEXT = re.compile(
    r"(?:all|full|complete|more|show|view|see|expand|open)\s+(?:the\s+)?"
    r"(?:technical\s+)?(?:specs?|specifications?|characteristics|features|details|technical\s+data)"
    r"|(?:specs?|specifications?|technical\s+data)\s*(?:[:\-–]\s*)?(?:show\s+)?(?:all|more)"
    r"|все\s+характеристики|показать\s+(?:все\s+)?характеристики|полные\s+характеристики|развернуть"
    r"|alle\s+(?:technischen\s+)?(?:daten|spezifikationen|eigenschaften)|technische\s+daten\s+anzeigen"
    r"|toutes\s+les\s+caract[eé]ristiques|voir\s+(?:toutes|plus)|ver\s+(?:todas|más)\s+(?:las\s+)?caracter[ií]sticas",
    re.IGNORECASE,
)
_SPEC_TAB_TEXT = re.compile(
    r"^\s*(?:tech(?:nical)?\s+)?(?:specs?|specifications?|characteristics|technical\s+data|data)\s*$"
    r"|^\s*характеристики\s*$|^\s*технические\s+характеристики\s*$"
    r"|^\s*technische\s+daten\s*$|^\s*caract[eé]ristiques(?:\s+techniques)?\s*$",
    re.IGNORECASE,
)
_HIDDEN_CLASS = re.compile(
    r"(?:^|[\s_-])(?:hidden|hide|d-none|is-hidden|collapsed?|closed|accordion[\s_-]*(?:content|body|panel)"
    r"|tab[\s_-]*pane|tabpanel|visually-hidden|invisible)(?:$|[\s_-])",
    re.IGNORECASE,
)
_ACTIVE_CLASS = re.compile(r"(?:^|\s)(?:active|show|open|in|is-open|current)(?:\s|$)", re.IGNORECASE)
_SPEC_CONTAINER = re.compile(
    r"(?<![a-z])spec|characteristic|(?<![a-z])propert|(?<![a-z])props?(?![a-z])|technical|tech-data"
    r"|(?<![a-z])attributes|(?<![a-z])parameters|характеристик|свойств",
    re.IGNORECASE,
)
_SPEC_JSON_KEY = re.compile(
    r'"(?:specifications?|specs|characteristics|technicalSpecs?|technicalData|productAttributes|'
    r'additionalProperty)"\s*:\s*[\[{]',
    re.IGNORECASE,
)
_KEY_VALUE_TEXT = re.compile(r"^[^:]{2,60}:\s*\S+")
_VOID = {"br", "hr", "img", "input", "link", "meta", "source", "wbr", "area", "base", "col", "embed", "param", "track"}
_SHELL_ROOTS = {"root", "app", "__next", "__nuxt", "app-root", "___gatsby"}


@dataclass(frozen=True, slots=True)
class PageInspection:
    url: str = ""
    has_expandable_specs: bool = False
    has_hidden_spec_content: Tri = "unknown"
    requires_interaction: Tri = "unknown"
    spec_controls: tuple[str, ...] = ()
    signals: tuple[str, ...] = ()
    spec_rows_visible: int = 0
    spec_rows_hidden: int = 0
    json_spec_blocks: int = 0
    js_shell: bool = False
    # Where the specifications live: visible_dom | dom_hidden | json_state |
    # js_required | unknown (the four cases the Stage 33.2 benchmark separates).
    spec_location: str = "unknown"

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(slots=True)
class _Frame:
    tag: str
    hidden: bool
    spec_container: bool


class _DomScanner(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[_Frame] = []
        self.controls: list[str] = []
        self.signals: list[str] = []
        self.rows_visible = 0
        self.rows_hidden = 0
        self.json_blocks = 0
        self.anchor_targets: set[str] = set()
        self.ids: set[str] = set()
        self.body_text_len = 0
        self.shell_root = False
        self.details_closed_spec = 0
        self._text_target: dict[str, object] | None = None
        self._script_type = ""
        self._script_buf: list[str] = []
        self._tab_text: list[str] | None = None
        self._tab_attrs: dict[str, str] = {}
        self._captures: list[tuple[str, dict[str, str], list[str]]] = []

    # -- helpers ---------------------------------------------------------
    @property
    def _in_hidden(self) -> bool:
        return any(frame.hidden for frame in self.stack)

    @property
    def _in_spec(self) -> bool:
        return any(frame.spec_container for frame in self.stack)

    def _record_row(self) -> None:
        if not self._in_spec:
            return
        if self._in_hidden:
            self.rows_hidden += 1
        else:
            self.rows_visible += 1

    # -- parser hooks ----------------------------------------------------
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.lower(): (value or "") for key, value in attrs}
        classes = values.get("class", "")
        identifier = values.get("id", "")
        if identifier:
            self.ids.add(identifier)
            if identifier in _SHELL_ROOTS:
                self.shell_root = True
        style = re.sub(r"\s+", "", values.get("style", "").lower())
        hidden = (
            "hidden" in values
            or "display:none" in style
            or "visibility:hidden" in style
            or values.get("aria-hidden", "").lower() == "true"
            or (tag == "details" and "open" not in values)
            or tag == "template"
        )
        weak_hidden = bool(_HIDDEN_CLASS.search(classes)) and not _ACTIVE_CLASS.search(classes)
        spec_marker = bool(_SPEC_CONTAINER.search(f"{identifier} {classes} {values.get('data-name', '')}"))
        if hidden:
            reason = (
                "hidden attribute" if "hidden" in values
                else "display:none" if "display:none" in style
                else "closed <details>" if tag == "details"
                else "<template>" if tag == "template"
                else "aria-hidden"
            )
            if spec_marker or tag in {"details", "template"}:
                self.signals.append(f"{reason} on <{tag}{'#' + identifier if identifier else ''}>")
        if weak_hidden and spec_marker:
            self.signals.append(f"collapsed container class '{classes.strip()[:40]}'")
        if tag not in _VOID:
            self.stack.append(_Frame(tag, hidden or (weak_hidden and spec_marker), spec_marker))

        if values.get("aria-expanded", "").lower() == "false":
            self.signals.append("aria-expanded=false control")
        if values.get("data-toggle") == "collapse" or values.get("data-bs-toggle") == "collapse":
            self.signals.append("collapse toggle control")
        if values.get("role") == "tab":
            self._tab_text = []
            self._tab_attrs = values
        interactive = tag in {"a", "button", "summary"} or any(
            key in values for key in ("role", "aria-expanded", "aria-controls", "data-toggle", "data-bs-toggle")
        )
        if interactive and tag not in _VOID:
            self._captures.append((tag, values, []))
        if tag == "a" and values.get("href", "").startswith("#") and len(values["href"]) > 1:
            self.anchor_targets.add(values["href"][1:])
        if values.get("data-target", "").startswith("#"):
            self.anchor_targets.add(values["data-target"][1:])
        if tag == "script":
            self._script_type = values.get("type", "").lower()
            self._script_buf = []
        if tag in {"tr", "dt"}:
            self._record_row()

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            body = "".join(self._script_buf)
            if body and (
                "json" in self._script_type or self._script_type == ""
                and ("__NEXT_DATA__" in body[:200] or "INITIAL_STATE" in body[:400])
            ) and _SPEC_JSON_KEY.search(body[:2_000_000]):
                self.json_blocks += 1
            elif body and "ld+json" in self._script_type:
                self._scan_ld_json(body)
            self._script_type = ""
            self._script_buf = []
        if self._captures and self._captures[-1][0] == tag:
            _, attrs, parts = self._captures.pop()
            text = " ".join(" ".join(parts).split())
            expandable_attr = (
                attrs.get("role") == "tab"
                or "aria-expanded" in attrs
                or attrs.get("aria-controls")
                or attrs.get("data-toggle") in {"tab", "collapse"}
                or attrs.get("data-bs-toggle") in {"tab", "collapse"}
            )
            if text and len(text) <= 80 and _CONTROL_TEXT.search(text):
                self.controls.append(text)
            elif text and len(text) <= 40 and _SPEC_TAB_TEXT.search(text) and expandable_attr:
                self.controls.append(text)
        if self._tab_text is not None and tag in {"a", "button", "li", "div", "span"}:
            self._tab_text = None
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                break

    def handle_data(self, data: str) -> None:
        if self._script_type is not None and self.stack and self.stack[-1].tag == "script":
            self._script_buf.append(data)
            return
        stripped = data.strip()
        if not stripped:
            return
        self.body_text_len += len(stripped)
        for _, _, parts in self._captures:
            if len(parts) < 40:
                parts.append(stripped)
        if self._in_spec and _KEY_VALUE_TEXT.match(stripped) and (
            not self.stack or self.stack[-1].tag not in {"td", "dd", "th", "dt"}
        ):
            self._record_row()

    def _scan_ld_json(self, body: str) -> None:
        try:
            payload = json.loads(body)
        except (json.JSONDecodeError, ValueError):
            return
        pending = [payload]
        while pending:
            node = pending.pop()
            if isinstance(node, list):
                pending.extend(node)
            elif isinstance(node, dict):
                if node.get("additionalProperty"):
                    self.json_blocks += 1
                pending.extend(node.values())


def inspect_product_page(html: str, url: str = "") -> PageInspection:
    """Diagnose how the page stores its specifications; never clicks or extracts."""
    scanner = _DomScanner()
    try:
        scanner.feed((html or "")[:6_000_000])
        scanner.close()
    except Exception:  # noqa: BLE001 - malformed markup must not fail discovery
        return PageInspection(url=url, signals=("html could not be parsed",))

    controls = tuple(dict.fromkeys(scanner.controls))
    target_in_dom = sorted(target for target in scanner.anchor_targets if target in scanner.ids)
    has_expandable = bool(controls) or any(
        signal.startswith(("aria-expanded", "collapse toggle", "closed <details>"))
        for signal in scanner.signals
    ) and bool(_SPEC_CONTAINER.search(" ".join(scanner.signals + list(controls))) or controls)
    signals = list(dict.fromkeys(scanner.signals))
    if controls:
        signals.insert(0, f"spec control: '{controls[0]}'")
    if target_in_dom and controls:
        signals.append(f"control targets in-page sections present in DOM: {', '.join(target_in_dom[:3])}")

    shell = scanner.shell_root and scanner.body_text_len < 600
    hidden_dom = scanner.rows_hidden > 0 or any(
        marker in signal for signal in signals
        for marker in ("hidden attribute", "display:none", "closed <details>", "<template>", "collapsed container")
    )
    if scanner.rows_hidden > 0 or scanner.json_blocks > 0:
        hidden_state: Tri = "true"
    elif shell:
        hidden_state = "unknown"
    elif hidden_dom or (has_expandable and target_in_dom and scanner.rows_visible > 0):
        hidden_state = "true"
    else:
        hidden_state = "false"

    spec_in_html = (
        scanner.rows_hidden + scanner.rows_visible >= 3
        or scanner.json_blocks > 0
    )
    if shell:
        interaction: Tri = "unknown"
    elif has_expandable and not spec_in_html and not target_in_dom:
        interaction = "true"
    elif spec_in_html or not has_expandable:
        interaction = "false"
    else:
        interaction = "unknown"
    if interaction == "true":
        signals.append("spec content absent from delivered HTML; a click/XHR is required")
    if shell:
        location = "unknown"
    elif interaction == "true":
        location = "js_required"
    elif scanner.rows_hidden > 0 or (hidden_dom and scanner.rows_visible == 0 and scanner.json_blocks == 0):
        location = "dom_hidden"
    elif scanner.json_blocks > 0:
        location = "json_state"
    elif scanner.rows_visible > 0:
        location = "visible_dom"
    else:
        location = "unknown"
    return PageInspection(
        url=url,
        has_expandable_specs=has_expandable,
        has_hidden_spec_content=hidden_state,
        requires_interaction=interaction,
        spec_controls=controls,
        signals=tuple(signals[:12]),
        spec_rows_visible=scanner.rows_visible,
        spec_rows_hidden=scanner.rows_hidden,
        json_spec_blocks=scanner.json_blocks,
        js_shell=shell,
        spec_location=location,
    )


_FETCH_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/140.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.5",
    "Accept-Language": "en;q=0.9,ru;q=0.6",
}


def _drop_default_port(url: str) -> str:
    return re.sub(r"^(https?://[^/:]+):(?:443|80)(?=/|$)", lambda match: match.group(1), url)


def fetch_page_html(
    url: str, *, timeout: float = 12.0, max_bytes: int = 4 * 1024 * 1024,
    session: object | None = None, attempts: list[dict[str, object]] | None = None,
) -> tuple[str, str] | None:
    """Bounded GET of one official page.  Returns ``(final_url, html)`` or ``None``."""
    import requests

    client = session or requests
    started = time.monotonic()

    def record(status: str, http_status: int | None = None) -> None:
        if attempts is not None:
            attempts.append({"url": url, "status": status, "http_status": http_status,
                             "duration_seconds": round(time.monotonic() - started, 3)})

    try:
        response = client.get(  # type: ignore[union-attr]
            url, headers=_FETCH_HEADERS, timeout=timeout, allow_redirects=True, stream=True,
        )
    except requests.RequestException as error:
        record("timeout" if isinstance(error, requests.Timeout) else "connection_error"
               if isinstance(error, requests.ConnectionError) else "request_error")
        return None
    try:
        http_status = int(getattr(response, "status_code", 0))
        if http_status >= 400:
            record("http_blocked" if http_status in {401, 403, 429} else "http_error", http_status)
            return None
        content_type = str(getattr(response, "headers", {}).get("content-type", "")).lower()
        if content_type and "html" not in content_type and "xml" not in content_type and "json" not in content_type:
            record("unsupported_content_type", http_status)
            return None
        try:
            if isinstance(response, requests.Response):
                chunks: list[bytes] = []
                size = 0
                for chunk in response.iter_content(64 * 1024):
                    chunks.append(chunk)
                    size += len(chunk)
                    if size >= max_bytes:
                        break
                body = b"".join(chunks)[:max_bytes]
                encoding = response.encoding or "utf-8"
                try:
                    text = body.decode(encoding, errors="replace")
                except LookupError:
                    text = body.decode("utf-8", errors="replace")
            else:
                text = str(getattr(response, "text", "") or "")[:max_bytes]
        except requests.RequestException as error:
            record("timeout" if isinstance(error, requests.Timeout) else "body_read_error", http_status)
            return None
        record("loaded", http_status)
        return _drop_default_port(str(getattr(response, "url", url))), text
    finally:
        close = getattr(response, "close", None)
        if close is not None:
            close()


def url_variants(url: str) -> list[str]:
    """The URL as given plus its trailing-slash twin.

    Canonicalisation drops a trailing slash so duplicates merge, but some
    platforms answer only one of the two spellings (a 404 on the other).
    """
    from urllib.parse import urlparse

    parsed = urlparse(url)
    last = parsed.path.rsplit("/", 1)[-1]
    if parsed.query or "." in last:
        return [url]
    twin = url[:-1] if url.endswith("/") else f"{url}/"
    return [url, twin]


def fetch_working_page(
    url: str, *, timeout: float = 12.0, session: object | None = None,
    attempts: list[dict[str, object]] | None = None,
) -> tuple[str, str] | None:
    """Fetch both slash spellings within one shared request deadline."""
    deadline = time.monotonic() + max(0.0, timeout)
    for variant in url_variants(url):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        page = fetch_page_html(variant, timeout=remaining, session=session, attempts=attempts)
        if page is not None:
            return page
    return None
