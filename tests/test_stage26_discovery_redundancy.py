"""Stage 26 deterministic independent-discovery redundancy coverage."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

import requests

from core.discovery import (
    DirectDomainProbeProvider,
    ProviderAttempt,
    ProviderQueryOutcome,
    ResilientSearchSession,
    SearchResultRecord,
    SeznamSearchProvider,
    build_provider_query,
    discover_with_status,
)


class ScriptedProvider:
    def __init__(self, name, response, *, always_run=False, quality_gate=False):
        self.name = name
        self.response = response
        self.always_run = always_run
        self.quality_gate = quality_gate
        self.calls: list[str] = []

    def search_with_timeout(self, query, _timeout):
        self.calls.append(query)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response

    def search(self, query):
        return self.search_with_timeout(query, 8.0)


class FakeResponse:
    def __init__(self, url: str, text: str, status_code: int = 200):
        self.url = url
        self.text = text
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))


class Stage26DiscoveryRedundancyTests(unittest.TestCase):
    def test_provider_specific_query_construction(self) -> None:
        query = "Acme X100 specifications"
        self.assertEqual(
            build_provider_query("bing", query, "Acme", "X100"),
            'Acme "X100" specifications',
        )
        self.assertEqual(
            build_provider_query("naver", 'Acme "X100" specifications', "Acme", "X100"),
            "Acme X100 specifications",
        )
        self.assertEqual(build_provider_query("duckduckgo_html", query, "Acme", "X100"), query)
        self.assertEqual(
            build_provider_query("bing", '"Acme X100"', "Acme", "X100"),
            '"Acme X100"',
        )

    def test_successful_serp_and_independent_path_are_both_merged(self) -> None:
        serp = ScriptedProvider(
            "duckduckgo_html", [("https://retailer.example/x100", "Acme X100")],
        )
        direct = ScriptedProvider(
            "direct", [("https://acme.example/products/x100", "Acme X100")],
            always_run=True,
        )
        session = ResilientSearchSession(providers=(serp, direct))
        session.configure_identity("Acme", "X100")

        outcome = session.search_with_status("Acme X100")

        self.assertEqual(
            [item.url for item in outcome.results],
            ["https://retailer.example/x100", "https://acme.example/products/x100"],
        )
        self.assertEqual([item.provider for item in outcome.attempts], ["duckduckgo_html", "direct"])

    def test_one_alternative_timeout_does_not_block_independent_path(self) -> None:
        slow = ScriptedProvider("alternative", TimeoutError("deadline exhausted"))
        direct = ScriptedProvider(
            "direct", [("https://acme.example/products/x100", "Acme X100")],
            always_run=True,
        )
        session = ResilientSearchSession(providers=(slow, direct))
        session.configure_identity("Acme", "X100")

        outcome = session.search_with_status("Acme X100")

        self.assertEqual([item.status for item in outcome.attempts], ["timeout", "success"])
        self.assertTrue(outcome.attempts[-1].independent_success_without_ddg)

    def test_direct_probe_finds_exact_model_from_sitemap_without_serp(self) -> None:
        http = requests.Session()
        provider = DirectDomainProbeProvider("global", session=http)
        provider.configure_identity("Acme", "X100")

        def get(url, **_kwargs):
            if url.endswith("robots.txt"):
                return FakeResponse(url, "Sitemap: https://www.acme.com/sitemap.xml")
            if url.endswith("sitemap.xml"):
                return FakeResponse(
                    url,
                    "<urlset><url><loc>https://www.acme.com/products/x100</loc></url></urlset>",
                )
            if "/search?" in url:
                return FakeResponse(url, "<html></html>")
            return FakeResponse(
                "https://www.acme.com/",
                "<html><head><title>Acme Home</title></head></html>",
            )

        with patch.object(http, "get", side_effect=get):
            results = provider.search("Acme X100 specifications")

        self.assertIn("https://acme.com/products/x100", [item.url for item in results])
        self.assertLessEqual(provider._request_count, provider.max_requests)

    def test_candidate_dedupe_preserves_cross_provider_provenance(self) -> None:
        def searcher(query):
            return ProviderQueryOutcome(
                (
                    SearchResultRecord(
                        "https://www.acme.com/products/x100?utm_source=a",
                        "Acme X100", provider="first", query=query,
                    ),
                    SearchResultRecord(
                        "https://acme.com/products/x100",
                        "Acme X100 specifications", provider="second", query=query,
                    ),
                ),
                (ProviderAttempt("merged", query, "success", result_count=2),),
            )

        outcome = discover_with_status("Acme", "X100", searcher=searcher)

        matching = [item for item in outcome.candidates if "/products/x100" in item["url"]]
        self.assertEqual(len(matching), 1)
        self.assertEqual(
            {item["provider"] for item in matching[0]["discovery_provenance"]},
            {"first", "second"},
        )

    def test_ddg_can_be_disabled_without_removing_other_providers(self) -> None:
        with patch.dict(os.environ, {"PDV_DISABLED_DISCOVERY_PROVIDERS": "duckduckgo_html"}):
            session = ResilientSearchSession()
        names = [provider.name for provider in session.providers]
        self.assertNotIn("duckduckgo_html", names)
        self.assertIn("direct_domain_probe", names)
        self.assertIn("bing", names)
        self.assertIn("seznam", names)

    def test_seznam_provider_returns_external_exact_model_results(self) -> None:
        provider = SeznamSearchProvider()
        html = """
            <a href="https://search.seznam.cz/help">navigation</a>
            <a href="https://shop.example/products/acme-x100">Acme X100 specifications</a>
        """
        response = FakeResponse("https://search.seznam.cz/", html)
        with patch("core.discovery.requests.get", return_value=response):
            results = provider.search("Acme X100")
        self.assertEqual([item.url for item in results], ["https://shop.example/products/acme-x100"])


if __name__ == "__main__":
    unittest.main()
