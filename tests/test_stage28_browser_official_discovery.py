"""Deterministic coverage for Stage 28 browser-backed official discovery."""

from __future__ import annotations

import unittest

from core.discovery import (
    BrowserOfficialDiscoveryProvider,
    ResilientSearchSession,
    SearchResultRecord,
    RenderedPage,
    clear_official_domain_cache,
    discover_with_status,
)


HOMEPAGE_HTML = "<html><head><title>Acme Home</title></head><body>Acme site</body></html>"


class FakeRenderer:
    """Deterministic stand-in for a real Chromium session, keyed by exact URL."""

    def __init__(self, routes: dict[str, RenderedPage]) -> None:
        self.routes = routes
        self.calls: list[str] = []
        self.closed = False

    def render(self, url: str, timeout_seconds: float) -> RenderedPage:
        self.calls.append(url)
        page = self.routes.get(url)
        if page is None:
            return RenderedPage(url=url, status_code=404, html="not found")
        return page

    def close(self) -> None:
        self.closed = True


class RaisingRenderer:
    """Renderer whose first navigation always raises (simulated timeout)."""

    def __init__(self, error: Exception) -> None:
        self.error = error
        self.calls: list[str] = []

    def render(self, url: str, timeout_seconds: float) -> RenderedPage:
        self.calls.append(url)
        raise self.error

    def close(self) -> None:
        return None


class ScriptedProvider:
    def __init__(
        self,
        name: str,
        results,
        *,
        short_circuit_on_exact_model: bool = False,
    ) -> None:
        self.name = name
        self.results = results
        self.short_circuit_on_exact_model = short_circuit_on_exact_model
        self.calls: list[str] = []

    def search_with_timeout(self, query, _timeout):
        self.calls.append(query)
        return self.results

    def search(self, query):
        return self.search_with_timeout(query, 8.0)


def _make_provider(routes: dict[str, RenderedPage], **kwargs) -> tuple[
    BrowserOfficialDiscoveryProvider, FakeRenderer,
]:
    renderer = FakeRenderer(routes)
    provider = BrowserOfficialDiscoveryProvider(
        "global", renderer_factory=lambda: renderer, **kwargs,
    )
    provider.configure_identity("Acme", "X100")
    return provider, renderer


class Stage28BrowserDiscoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        clear_official_domain_cache()

    def test_rendered_dom_yields_exact_product_after_http_fails(self) -> None:
        routes = {
            "https://www.acme.com/": RenderedPage(
                url="https://www.acme.com/",
                status_code=200,
                html=(
                    "<html><head><title>Acme Home</title></head><body>"
                    '<a href="/products/x100">Acme X100</a>'
                    "</body></html>"
                ),
            ),
        }
        provider, renderer = _make_provider(routes)

        results = provider.search("Acme X100 specifications")

        self.assertIn("https://acme.com/products/x100", [item.url for item in results])
        self.assertTrue(provider.last_browser_invoked)
        self.assertEqual(provider.last_discovery_method, "rendered_homepage")
        self.assertEqual(provider.last_exact_model_candidate_count, 1)
        self.assertEqual(renderer.calls, ["https://www.acme.com/"])

    def test_official_first_then_browser_short_circuits_serp(self) -> None:
        official = ScriptedProvider(
            "direct_domain_probe",
            [("https://acme.com/", "Acme")],
            short_circuit_on_exact_model=True,
        )
        routes = {
            "https://www.acme.com/": RenderedPage(
                url="https://www.acme.com/",
                status_code=200,
                html=(
                    "<html><head><title>Acme Home</title></head><body>"
                    '<a href="/products/x100">Acme X100</a>'
                    "</body></html>"
                ),
            ),
        }
        browser, renderer = _make_provider(routes)
        serp = ScriptedProvider(
            "duckduckgo_html",
            [("https://retailer.example/x100", "Acme X100")],
        )
        session = ResilientSearchSession(providers=(official, browser, serp))
        session.configure_identity("Acme", "X100")

        outcome = session.search_with_status("Acme X100")

        self.assertEqual(serp.calls, [])
        self.assertEqual(
            [item.provider for item in outcome.attempts],
            ["direct_domain_probe", "browser_official_discovery"],
        )
        self.assertIn(
            "https://acme.com/products/x100",
            [item.url for item in outcome.results],
        )

    def test_browser_finds_product_via_rendered_site_search(self) -> None:
        routes = {
            "https://www.acme.com/": RenderedPage(
                url="https://www.acme.com/",
                status_code=200,
                html=HOMEPAGE_HTML,
            ),
            "https://acme.com/search?q=X100": RenderedPage(
                url="https://acme.com/search?q=X100",
                status_code=200,
                html=(
                    '<html><body><a href="/products/x100">Acme X100</a></body></html>'
                ),
            ),
        }
        provider, renderer = _make_provider(routes)

        results = provider.search("Acme X100 specifications")

        self.assertIn("https://acme.com/products/x100", [item.url for item in results])
        self.assertEqual(provider.last_discovery_method, "rendered_site_search")
        self.assertEqual(
            renderer.calls,
            ["https://www.acme.com/", "https://acme.com/search?q=X100"],
        )

    def test_browser_observes_public_json_xhr_product_result(self) -> None:
        routes = {
            "https://www.acme.com/": RenderedPage(
                url="https://www.acme.com/",
                status_code=200,
                html=HOMEPAGE_HTML,
                xhr_bodies=(
                    (
                        "https://www.acme.com/api/search",
                        '{"results":[{"name":"Acme X100","productUrl":"/products/x100"}]}',
                    ),
                ),
            ),
        }
        provider, renderer = _make_provider(routes)

        results = provider.search("Acme X100 specifications")

        self.assertIn("https://acme.com/products/x100", [item.url for item in results])
        matching = next(item for item in results if item.url.endswith("/products/x100"))
        self.assertEqual(matching.discovery_method, "xhr_json")
        self.assertEqual(provider.last_xhr_candidate_count, 1)
        # The homepage itself had no anchors, so only the XHR pass contributed.
        self.assertEqual(provider.last_rendered_candidate_count, 0)

    def test_candidate_passes_existing_identity_pipeline(self) -> None:
        official = ScriptedProvider(
            "direct_domain_probe",
            [("https://acme.com/", "Acme")],
            short_circuit_on_exact_model=True,
        )
        routes = {
            "https://www.acme.com/": RenderedPage(
                url="https://www.acme.com/",
                status_code=200,
                html=(
                    "<html><head><title>Acme Home</title></head><body>"
                    '<a href="/products/x100">Acme X100</a>'
                    "</body></html>"
                ),
            ),
        }
        browser, renderer = _make_provider(routes)
        serp = ScriptedProvider("duckduckgo_html", [])

        session = ResilientSearchSession(providers=(official, browser, serp))
        session.configure_identity("Acme", "X100")
        outcome = discover_with_status(
            "Acme", "X100", market="global", searcher=session.search_with_status,
        )

        matching = [item for item in outcome.candidates if "/products/x100" in item["url"]]
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0]["model_match"], "exact")
        self.assertIn(
            "browser_official_discovery",
            {item["provider"] for item in matching[0]["discovery_provenance"]},
        )
        rejected_urls = [item["url"] for item in outcome.rejected_candidates]
        self.assertNotIn("https://acme.com/products/x100", rejected_urls)

    def test_captcha_challenge_stops_without_bypass(self) -> None:
        routes = {
            "https://www.acme.com/": RenderedPage(
                url="https://www.acme.com/",
                status_code=200,
                html="<html><body>Please verify you are human to continue.</body></html>",
            ),
            "https://acme.com/search?q=X100": RenderedPage(
                url="https://acme.com/search?q=X100",
                status_code=200,
                html='<html><body><a href="/products/x100">Acme X100</a></body></html>',
            ),
        }
        provider, renderer = _make_provider(routes)

        results = provider.search("Acme X100 specifications")

        self.assertEqual(results, [])
        self.assertTrue(provider.last_captcha_detected)
        self.assertIn("blocked", provider.last_failure_reason or "")
        # The path stopped at the first challenge; the search page was never tried.
        self.assertEqual(renderer.calls, ["https://www.acme.com/"])

    def test_browser_timeout_lets_fallback_continue(self) -> None:
        renderer = RaisingRenderer(TimeoutError("navigation timed out"))
        provider = BrowserOfficialDiscoveryProvider(
            "global", renderer_factory=lambda: renderer,
        )
        provider.configure_identity("Acme", "X100")
        serp = ScriptedProvider(
            "duckduckgo_html",
            [("https://retailer.example/x100", "Acme X100")],
        )
        session = ResilientSearchSession(providers=(provider, serp))
        session.configure_identity("Acme", "X100")

        outcome = session.search_with_status("Acme X100")

        self.assertEqual(len(serp.calls), 1)
        self.assertIn(
            "https://retailer.example/x100",
            [item.url for item in outcome.results],
        )
        self.assertTrue(provider.last_browser_invoked)
        self.assertEqual(provider.last_exact_model_candidate_count, 0)

    def test_global_page_budget_is_respected(self) -> None:
        routes = {
            "https://www.acme.com/": RenderedPage(
                url="https://www.acme.com/",
                status_code=200,
                html=HOMEPAGE_HTML,
            ),
            "https://acme.com/search?q=X100": RenderedPage(
                url="https://acme.com/search?q=X100",
                status_code=200,
                html='<html><body><a href="/products/x100">Acme X100</a></body></html>',
            ),
        }
        provider, renderer = _make_provider(routes, max_pages=1)

        results = provider.search("Acme X100 specifications")

        self.assertEqual(results, [])
        self.assertEqual(provider.last_pages_opened, 1)
        self.assertEqual(renderer.calls, ["https://www.acme.com/"])

    def test_browser_not_invoked_when_http_already_found_exact_official(self) -> None:
        official = ScriptedProvider(
            "direct_domain_probe",
            [SearchResultRecord(
                "https://acme.com/products/x100",
                "Acme X100",
                discovery_method="sitemap",
            )],
            short_circuit_on_exact_model=True,
        )
        routes = {
            "https://www.acme.com/": RenderedPage(
                url="https://www.acme.com/",
                status_code=200,
                html=HOMEPAGE_HTML,
            ),
        }
        browser, renderer = _make_provider(routes)
        session = ResilientSearchSession(providers=(official, browser))
        session.configure_identity("Acme", "X100")

        outcome = session.search_with_status("Acme X100")

        self.assertEqual(renderer.calls, [])
        self.assertFalse(browser.last_browser_invoked)
        self.assertEqual(
            [item.provider for item in outcome.attempts], ["direct_domain_probe"],
        )

    def test_authority_is_not_auto_granted_by_browser_discovery(self) -> None:
        routes = {
            "https://www.acme.com/": RenderedPage(
                url="https://www.acme.com/",
                status_code=200,
                html=(
                    "<html><head><title>Acme Home</title></head><body>"
                    '<a href="/products/x100">Acme X100</a>'
                    "</body></html>"
                ),
            ),
        }
        provider, renderer = _make_provider(routes)

        provider.search("Acme X100 specifications")

        # Discovery never self-declares official authority -- rank_candidates
        # and the authority pipeline decide that afterwards, exactly like
        # DirectDomainProbeProvider.
        self.assertIsNone(provider.last_accepted_official_url)


if __name__ == "__main__":
    unittest.main()
