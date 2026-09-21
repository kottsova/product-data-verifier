"""Worker-side hooks for the Stage 36.7 hard-deadline tests.

Each ``scenario_*`` function is named by ``DiscoveryDebugService(worker_hooks=...)``
and runs inside the child process, where it may replace providers or patch the
page fetcher.  Nothing here is imported by production code.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import time

from core.discovery import SearchResultRecord

EXACT_URL = (
    "https://www.bosch-home.co.uk/en/product/laundry/washing-machines/"
    "front-load-washing-machine/WAN28254GB"
)
_SLEEPER = "import time; time.sleep(3600)"


def _note_pid(kind: str, pid: int) -> None:
    target = os.environ.get("PDV_TEST_PIDFILE")
    if target:
        with open(target, "a", encoding="utf-8") as handle:
            handle.write(f"{kind} {pid}\n")


def _spawn_sleeper(kind: str) -> subprocess.Popen:
    child = subprocess.Popen([sys.executable, "-c", _SLEEPER])
    _note_pid(kind, child.pid)
    return child


class FastProvider:
    """Returns one exact first-party product page immediately."""

    name = "fast_serp"
    always_run = True

    def search(self, query: str):
        return [SearchResultRecord(
            url=EXACT_URL, title="Bosch WAN28254GB washing machine",
            snippet="Bosch WAN28254GB front loader", provider=self.name, query=query,
        )]


class HangingProvider:
    """Ignores every timeout, like a socket stuck inside a native call."""

    name = "hanging_serp"
    always_run = True

    def search(self, query: str):
        _note_pid("worker", os.getpid())
        _spawn_sleeper("provider_child")
        while True:
            time.sleep(3600)


class SlowCleanupProvider:
    """Answers at once but takes minutes to shut down, with a stray child."""

    name = "slow_cleanup"
    always_run = True

    def __init__(self) -> None:
        self._child: subprocess.Popen | None = None

    def search(self, query: str):
        _note_pid("worker", os.getpid())
        if self._child is None:
            self._child = _spawn_sleeper("browser_like_child")
        return [SearchResultRecord(
            url=EXACT_URL, title="Bosch WAN28254GB washing machine",
            snippet="Bosch WAN28254GB", provider=self.name, query=query,
        )]

    def release_transient_resources(self) -> None:
        time.sleep(600)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        time.sleep(600)


def hang_in_serp() -> dict:
    return {"providers": lambda: [FastProvider(), HangingProvider()]}


def slow_cleanup() -> dict:
    return {"providers": lambda: [SlowCleanupProvider()]}


def fast_only() -> dict:
    return {"providers": lambda: [FastProvider()]}


def crash_in_worker() -> dict:
    raise RuntimeError("worker hook failed on purpose")


def _trickle_fetch(url: str, *, timeout: float = 12.0, session=None, attempts=None):
    """A page that never finishes downloading: a byte every 0.5s, no read timeout hit."""
    _note_pid("worker", os.getpid())
    while True:
        time.sleep(0.5)


def slow_page_load() -> dict:
    import services.discovery_debug as debug

    debug.fetch_working_page = _trickle_fetch
    return {"providers": lambda: [FastProvider()]}


def real_chromium_slow_close() -> dict:
    """Launch a real Playwright Chromium, then never close it in time."""
    from playwright.sync_api import sync_playwright

    class BrowserProvider(FastProvider):
        name = "real_browser"

        def __init__(self) -> None:
            self._pw = None
            self._browser = None

        def search(self, query: str):
            if self._browser is None:
                _note_pid("worker", os.getpid())
                self._pw = sync_playwright().start()
                self._browser = self._pw.chromium.launch(headless=True)
            return super().search(query)

        def __enter__(self):
            return self

        def __exit__(self, *_):
            time.sleep(600)

    return {"providers": lambda: [BrowserProvider()]}


HOOKS_DIR = Path(__file__).resolve().parent


def saved_case() -> dict:
    """One saved candidate plus its transcribed primary HTML (env PDV_TEST_CASE)."""
    import json
    import services.discovery_debug as debug

    case = json.loads(os.environ["PDV_TEST_CASE"])

    class Saved:
        name = "saved_candidate"

        def search(self, query: str):
            return [(case["url"], case["title"])]

    debug.fetch_working_page = lambda url, **_: (case["url"], case["html"])
    return {"providers": lambda: [Saved()], "document_reader": lambda _url: None}
