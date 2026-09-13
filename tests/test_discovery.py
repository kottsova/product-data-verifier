import unittest
from unittest.mock import patch

from core.discovery import (
    GoogleSearchSession,
    browser_headless,
    canonicalize_url,
    classify_source,
    clear_official_domain_cache,
    discover,
    is_obvious_non_product_url,
    rank_candidates,
)
from core.match import candidate_model_match, model_match, normalize_model


class MatchTests(unittest.TestCase):
    def test_normalize_model(self) -> None:
        self.assertEqual(normalize_model(" pue-611 bb5e "), "PUE611BB5E")

    def test_exact_match(self) -> None:
        self.assertEqual(model_match("PUE611BB5E", "Bosch PUE611BB5E Series 6"), "exact")

    def test_likely_revision_match(self) -> None:
        self.assertEqual(model_match("PUE611BB5E", "PUE611BB5E/01"), "likely_variant")

    def test_regional_variant_match(self) -> None:
        self.assertEqual(model_match("WW90T554CAT", "WW90T554CAT/LP"), "likely_variant")
        self.assertEqual(model_match("WW90T554CAT", "WW90T554CAT/LD"), "likely_variant")

    def test_other_sku_is_not_a_variant(self) -> None:
        self.assertNotEqual(model_match("WW90T554CAT", "WW10T554DAW/S1"), "likely_variant")
        self.assertNotEqual(model_match("WW90T554CAT", "WW5500T"), "likely_variant")

    def test_compact_extension_is_not_automatically_a_variant(self) -> None:
        self.assertNotEqual(model_match("WW90T554CAT", "WW90T554CATX"), "likely_variant")

    def test_other_titled_sku_blocks_incidental_url_variant(self) -> None:
        result = candidate_model_match(
            "WW90T554CAT",
            "WW5500T (WW10T554DAW/S1) with Eco Bubble",
            "/washing-machines/ww90t554cat-ld",
        )
        self.assertNotEqual(result, "likely_variant")

    def test_file_extension_is_not_a_variant(self) -> None:
        self.assertEqual(model_match("PUE611BB5E", "PUE611BB5E.pdf"), "exact")

    def test_mismatch(self) -> None:
        self.assertEqual(model_match("PUE611BB5E", "PUE611BB5F"), "mismatch")


class DiscoveryTests(unittest.TestCase):
    def test_url_deduplication(self) -> None:
        results = [
            ("https://example.com/product/PUE611BB5E?utm_source=x", "PUE611BB5E"),
            ("https://www.example.com/product/PUE611BB5E/", "PUE611BB5E duplicate"),
        ]
        candidates = rank_candidates(results, "Bosch", "PUE611BB5E")
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["url"], "https://example.com/product/PUE611BB5E")

    def test_marketplace_classification(self) -> None:
        self.assertEqual(classify_source("amazon.com", None), "marketplace")

    def test_regional_marketplaces(self) -> None:
        for domain in ("amazon.fr", "amazon.co.uk", "wildberries.ge"):
            with self.subTest(domain=domain):
                self.assertEqual(classify_source(domain, None), "marketplace")

    def test_filters_obvious_non_product_urls(self) -> None:
        self.assertTrue(is_obvious_non_product_url("https://example.com/about/company"))
        self.assertTrue(is_obvious_non_product_url("https://youtube.com/watch?v=1"))
        self.assertFalse(is_obvious_non_product_url("https://example.com/product/PUE611BB5E"))

    def test_ranking_prefers_manufacturer_product(self) -> None:
        results = [
            ("https://amazon.com/dp/PUE611BB5E", "Bosch PUE611BB5E"),
            ("https://bosch-home.com/product/PUE611BB5E", "Bosch PUE611BB5E"),
        ]
        candidates = rank_candidates(results, "Bosch", "PUE611BB5E", official_domain="bosch-home.com")
        self.assertEqual(candidates[0]["source_type"], "manufacturer")

    def test_retailer_is_not_verified_without_evidence(self) -> None:
        candidates = rank_candidates(
            [("https://mvideo.ru/product/PUE611BB5E", "Bosch PUE611BB5E")],
            "Bosch", "PUE611BB5E",
        )
        self.assertEqual(candidates[0]["source_type"], "retailer")
        self.assertEqual(candidates[0]["authority_status"], "unknown")

    def test_manufacturer_with_evidence_is_verified_and_ranks_first(self) -> None:
        evidence = "https://bosch-home.com/"
        candidates = rank_candidates([
            ("https://amazon.fr/dp/PUE611BB5E", "Bosch PUE611BB5E"),
            ("https://bosch-home.com/product/PUE611BB5E", "Bosch PUE611BB5E"),
        ], "Bosch", "PUE611BB5E", official_domain="bosch-home.com",
            authority_evidence_url=evidence)
        self.assertEqual(candidates[0]["source_type"], "manufacturer")
        self.assertEqual(candidates[0]["authority_status"], "verified")
        self.assertEqual(candidates[0]["authority_evidence_url"], evidence)

    def test_exact_manufacturer_ranks_above_unknown_manufacturer(self) -> None:
        evidence = "https://example.com/"
        candidates = rank_candidates([
            ("https://example.com/product/X100", "Example X100"),
            ("https://example.com/smartphones", "Example smartphones"),
        ], "Example", "X100", official_domain="example.com",
            authority_evidence_url=evidence)
        self.assertEqual(candidates[0]["model_match"], "exact")
        self.assertGreater(candidates[0]["score"], candidates[1]["score"])

    def test_exact_retailer_ranks_above_unrelated_manufacturer(self) -> None:
        candidates = rank_candidates([
            ("https://mvideo.ru/product/X100", "Example X100"),
            ("https://example.com/smartphones", "Example smartphones"),
        ], "Example", "X100", official_domain="example.com",
            authority_evidence_url="https://example.com/")
        self.assertEqual(candidates[0]["source_type"], "retailer")
        self.assertEqual(candidates[0]["model_match"], "exact")

    def test_absent_gtin_does_not_downgrade_exact_model(self) -> None:
        candidate = rank_candidates([
            ("https://shop.example/product/X100", "Example X100"),
        ], "Example", "X100")[0]
        self.assertEqual(candidate["model_match"], "exact")
        self.assertGreater(candidate["score"], 0)

    def test_global_market_has_no_tld_bonus(self) -> None:
        candidates = rank_candidates([
            ("https://shop.ge/product/PUE611BB5E", "Bosch PUE611BB5E"),
            ("https://shop.de/product/PUE611BB5E", "Bosch PUE611BB5E"),
        ], "Bosch", "PUE611BB5E", market="global")
        self.assertEqual(candidates[0]["score"], candidates[1]["score"])

    def test_official_domain_cache(self) -> None:
        clear_official_domain_cache()
        calls = []

        def searcher(query):
            calls.append(query)
            if "official" in query:
                return [("https://acme.com/", "Acme official website")]
            return [("https://acme.com/product/X100", "Acme X100")]

        discover("Acme", "X100", searcher=searcher)
        first_official_calls = sum("official" in query for query in calls)
        calls.clear()
        discover("Acme", "X100", searcher=searcher)
        self.assertGreater(first_official_calls, 0)
        self.assertFalse(any("official" in query for query in calls))

    def test_headless_is_true_by_default(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            self.assertTrue(browser_headless())

    def test_canonical_url_keeps_functional_query(self) -> None:
        self.assertEqual(
            canonicalize_url("https://example.com/item?id=7&utm_campaign=x#specs"),
            "https://example.com/item?id=7",
        )


class SearchFallbackTests(unittest.TestCase):
    def test_http_organic_results_skip_playwright(self) -> None:
        session = GoogleSearchSession()
        organic = [("https://example.com/product/model", "Model")]
        with patch("core.discovery._http_google_search_for_market", return_value=(organic, "results")), \
                patch.object(session, "_playwright_search") as fallback:
            self.assertEqual(session.search("model"), organic)
            fallback.assert_not_called()

    def test_empty_http_results_use_playwright(self) -> None:
        session = GoogleSearchSession()
        fallback_results = [("https://example.com/product/model", "Model")]
        with patch("core.discovery._http_google_search_for_market", return_value=([], "empty")), \
                patch.object(session, "_playwright_search", return_value=fallback_results) as fallback:
            self.assertEqual(session.search("model"), fallback_results)
            fallback.assert_called_once_with("model")

    def test_consent_page_uses_playwright(self) -> None:
        session = GoogleSearchSession()
        misleading = [("https://example.com/product/model", "Model")]
        with patch(
            "core.discovery._http_google_search_for_market",
            return_value=(misleading, "Before you continue to Google"),
        ), patch.object(session, "_playwright_search", return_value=[]) as fallback:
            session.search("model")
            fallback.assert_called_once_with("model")


if __name__ == "__main__":
    unittest.main()
