"""Deterministic coverage for Stage 27 official-first discovery."""

from __future__ import annotations

import gzip
import unittest

import requests

from core.discovery import (
    DirectDomainProbeProvider,
    ResilientSearchSession,
    SearchResultRecord,
    clear_official_domain_cache,
)


class FakeResponse:
    def __init__(self, url: str, text: str, status_code: int = 200):
        self.url = url
        self.text = text
        self.status_code = status_code
        self.headers: dict[str, str] = {}

    def close(self) -> None:
        return None


def gzip_response(url: str, text: str) -> requests.Response:
    response = requests.Response()
    response.status_code = 200
    response.url = url
    response.headers["content-type"] = "application/x-gzip"
    response._content = gzip.compress(text.encode("utf-8"))
    response._content_consumed = True
    response.encoding = "utf-8"
    return response


class RoutingSession:
    def __init__(self, routes):
        self.routes = routes
        self.calls: list[str] = []

    def get(self, url, **_kwargs):
        self.calls.append(url)
        response = self.routes.get(url)
        if response is None:
            return FakeResponse(url, "not found", 404)
        return response


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


class Stage27OfficialFirstTests(unittest.TestCase):
    def setUp(self) -> None:
        clear_official_domain_cache()

    def test_exact_official_result_short_circuits_serp(self) -> None:
        official = ScriptedProvider(
            "direct_domain_probe",
            [SearchResultRecord(
                "https://acme.com/products/x100",
                "Acme X100",
                discovery_method="sitemap",
            )],
            short_circuit_on_exact_model=True,
        )
        serp = ScriptedProvider(
            "duckduckgo_html",
            [("https://retailer.example/x100", "Acme X100")],
        )
        session = ResilientSearchSession(providers=(official, serp))
        session.configure_identity("Acme", "X100")

        outcome = session.search_with_status("Acme X100")

        self.assertEqual(len(official.calls), 1)
        self.assertEqual(serp.calls, [])
        self.assertEqual([item.provider for item in outcome.attempts], ["direct_domain_probe"])

    def test_homepage_only_keeps_serp_as_fallback(self) -> None:
        official = ScriptedProvider(
            "direct_domain_probe",
            [("https://acme.com/", "Acme")],
            short_circuit_on_exact_model=True,
        )
        serp = ScriptedProvider(
            "duckduckgo_html",
            [("https://retailer.example/x100", "Acme X100")],
        )
        session = ResilientSearchSession(providers=(official, serp))
        session.configure_identity("Acme", "X100")

        outcome = session.search_with_status("Acme official website")

        self.assertEqual(len(serp.calls), 1)
        self.assertEqual(
            [item.provider for item in outcome.attempts],
            ["direct_domain_probe", "duckduckgo_html"],
        )

    def test_gzip_sitemap_index_is_traversed_within_limits(self) -> None:
        index_url = "https://acme.com/product-index.xml.gz"
        product_map = "https://acme.com/products-0001.xml.gz"
        routes = {
            "https://www.acme.com/": FakeResponse(
                "https://www.acme.com/",
                "<html><head><title>Acme Home</title></head></html>",
            ),
            "https://www.acme.com/robots.txt": FakeResponse(
                "https://www.acme.com/robots.txt",
                f"Sitemap: {index_url}",
            ),
            index_url: gzip_response(
                index_url,
                f"<sitemapindex><sitemap><loc>{product_map}</loc></sitemap></sitemapindex>",
            ),
            product_map: gzip_response(
                product_map,
                "<urlset><url><loc>https://www.acme.com/products/x100</loc></url></urlset>",
            ),
        }
        session = RoutingSession(routes)
        provider = DirectDomainProbeProvider("global", session=session)
        provider.configure_identity("Acme", "X100")

        results = provider.search("Acme X100 specifications")

        self.assertIn("https://acme.com/products/x100", [item.url for item in results])
        self.assertEqual(provider.last_discovery_method, "sitemap")
        self.assertLessEqual(provider.last_method_requests["sitemap"], 3)
        self.assertLessEqual(provider._request_count, provider.max_requests)

    def test_public_structured_search_payload_can_yield_exact_url(self) -> None:
        search_url = "https://acme.com/search?q=X100"
        routes = {
            "https://www.acme.com/": FakeResponse(
                "https://www.acme.com/",
                "<html><head><title>Acme Home</title></head></html>",
            ),
            search_url: FakeResponse(
                search_url,
                '<script>{"name":"Acme X100","productUrl":"/products/x100"}</script>',
            ),
        }
        session = RoutingSession(routes)
        provider = DirectDomainProbeProvider("global", session=session)
        provider.configure_identity("Acme", "X100")

        results = provider.search("Acme X100 specifications")

        self.assertIn("https://acme.com/products/x100", [item.url for item in results])
        matching = next(item for item in results if item.url.endswith("/products/x100"))
        self.assertEqual(matching.discovery_method, "public_structured_endpoint")

    def test_structure_cache_does_not_store_product_result(self) -> None:
        first_routes = {
            "https://www.acme.com/": FakeResponse(
                "https://www.acme.com/",
                "<html><head><title>Acme Home</title></head></html>",
            ),
            "https://acme.com/search?q=X100": FakeResponse(
                "https://acme.com/search?q=X100",
                '<a href="/products/x100">Acme X100</a>',
            ),
        }
        first = DirectDomainProbeProvider("global", session=RoutingSession(first_routes))
        first.configure_identity("Acme", "X100")
        self.assertTrue(first.search("Acme X100"))

        second_routes = {
            "https://www.acme.com/": FakeResponse(
                "https://www.acme.com/",
                "<html><head><title>Acme Home</title></head></html>",
            ),
            "https://acme.com/search?q=X200": FakeResponse(
                "https://acme.com/search?q=X200",
                '<a href="/products/x200">Acme X200</a>',
            ),
        }
        second = DirectDomainProbeProvider("global", session=RoutingSession(second_routes))
        second.configure_identity("Acme", "X200")

        results = second.search("Acme X200")

        self.assertIn("https://acme.com/products/x200", [item.url for item in results])
        self.assertNotIn("https://acme.com/products/x100", [item.url for item in results])


if __name__ == "__main__":
    unittest.main()
