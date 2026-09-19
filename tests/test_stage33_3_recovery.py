"""Stage 33.3: sibling official domains, learned SKU routes, POST search forms, DNS-gated roots."""

from __future__ import annotations

import time
import unittest
from unittest.mock import patch

from core.discovery import (
    FAMILY_LINK_MARKER,
    DirectDomainProbeProvider,
    SearchResultRecord,
    _DirectLinkParser,
    _FetchedOfficialSurface,
    discover_global_official_domains,
)


def page(url: str, html: str) -> _FetchedOfficialSurface:
    return _FetchedOfficialSurface(url, 200, html, "text/html")


class ParserTests(unittest.TestCase):
    def test_post_search_form_keeps_hidden_fields(self) -> None:
        parser = _DirectLinkParser()
        parser.feed(
            '<form method="post" action="/search/"><input type="hidden" name="a" value="search">'
            '<input type="search" name="searchTerm"></form>'
        )
        self.assertEqual(parser.search_forms, [("/search/?a=search", "searchTerm")])


class RouteTests(unittest.TestCase):
    def provider(self) -> DirectDomainProbeProvider:
        provider = DirectDomainProbeProvider()
        provider.configure_identity("Acme", "HBG7741B1")
        return provider

    def test_routes_and_locales_are_learned_from_sitemap_urls(self) -> None:
        provider = self.provider()
        provider._learn_routes([
            "https://www.acme-home.com/de/de/product/cooking/oven/00016229",
            "https://www.acme-home.com/de/de/product/cooking/oven/00016230",
            "https://www.acme-home.com/ch/fr/category/ovens",
            "https://www.acme-home.com/ch/fr/sitemap.xml",
        ])
        self.assertIn("https://www.acme-home.com/de/de/product", provider._routes)
        self.assertIn("https://www.acme-home.com/ch/fr", provider._locales)
        self.assertNotIn("https://www.acme-home.com/ch/fr/category", provider._routes)

    def test_route_probe_needs_the_page_itself_to_name_the_sku(self) -> None:
        provider = self.provider()
        provider._learn_routes([
            "https://www.acme-home.com/de/de/product/a/00016229",
            "https://www.acme-home.com/de/de/product/b/00016230",
        ])
        good = (
            '<html><head><title>HBG7741B1 Oven | Acme</title>'
            '<link rel="canonical" href="https://www.acme-home.com/de/de/product/ovens/HBG7741B1"/></head></html>'
        )
        echo = "<html><head><title>No results</title></head></html>"  # 200 page echoing the request
        with patch.object(provider, "_get", side_effect=lambda url, *_: page(url, good if "/de/de/" in url else echo)):
            records = provider._sku_route_records("acme-home.com", time.monotonic() + 5)
        self.assertEqual([item.url for item in records], ["https://acme-home.com/de/de/product/ovens/HBG7741B1"])
        with patch.object(provider, "_get", side_effect=lambda url, *_: page(url, echo)):
            self.assertEqual(provider._sku_route_records("acme-home.com", time.monotonic() + 5), [])


class FamilyTests(unittest.TestCase):
    def test_ecosystem_with_many_regional_roots_ranks_first_and_press_sites_are_skipped(self) -> None:
        provider = DirectDomainProbeProvider()
        provider.configure_identity("Acme", "HBG7741B1")
        html = " ".join(
            [f'<a href="https://www.acme-home.{tld}/x">h</a>' for tld in ("com", "de", "at", "be")]
            + ['<a href="https://www.acme-diy.com/">d</a>', '<a href="https://www.acme-presse.de/">p</a>',
               '<a href="https://www.acme-shop.com/">s</a>', '<a href="https://www.acme.de/">own</a>']
            + [r'{"url": "https:\/\/www.acme-motor.com\/x"}']
        )
        provider._harvest_family(html, "https://www.acme.com/", "acme.com")
        self.assertEqual(
            provider._family_candidates(set())[:3], ["acme-home.com", "acme-diy.com", "acme-motor.com"],
        )
        self.assertNotIn("acme-presse.de", provider._family_hits)
        self.assertNotIn("acme-shop.com", provider._family_hits)

    def test_garbage_urls_in_page_state_never_raise(self) -> None:
        provider = DirectDomainProbeProvider()
        provider.configure_identity("Acme", "HBG7741B1")
        provider._harvest_family('{"a": "https://x:"TRUE"}}"} https://www.acme-home.com/', "https://www.acme.com/", "acme.com")
        self.assertIn("acme-home.com", provider._family_hits)

    def test_dns_gate_only_lets_live_names_through(self) -> None:
        provider = DirectDomainProbeProvider()
        provider._dns_lookup = lambda host, port: [1] if host.endswith("acme.co.nz") else (_ for _ in ()).throw(OSError())
        self.assertEqual(provider._dns_live_domains("Acme", set()), ["acme.co.nz"])


class AuthorityTests(unittest.TestCase):
    def record(self, url: str, snippet: str) -> SearchResultRecord:
        return SearchResultRecord(
            url=url, title="HBG7741B1 Oven | Acme Home", snippet=snippet,
            provider="direct_domain_probe", discovery_method="sku_route_template",
        )

    def test_sibling_site_needs_the_cross_link_marker(self) -> None:
        url = "https://www.acme-home.com/de/de/product/ovens/HBG7741B1"
        with_marker = self.record(url, f"Requested SKU confirmed; brand confirmed in fetched page; {FAMILY_LINK_MARKER}.")
        without = self.record(url, "Requested SKU confirmed; brand confirmed in fetched page.")
        found = discover_global_official_domains("Acme", [], product_results=[with_marker], model="HBG7741B1")
        self.assertEqual([domain for domain, _ in found], ["acme-home.com"])
        self.assertEqual(discover_global_official_domains("Acme", [], product_results=[without], model="HBG7741B1"), [])

    def test_commerce_word_siblings_are_never_official(self) -> None:
        record = self.record(
            "https://www.acme-shop.com/p/HBG7741B1", f"brand confirmed in fetched page; {FAMILY_LINK_MARKER}.",
        )
        self.assertEqual(discover_global_official_domains("Acme", [], product_results=[record], model="HBG7741B1"), [])


if __name__ == "__main__":
    unittest.main()
