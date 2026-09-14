import unittest
from unittest.mock import patch

from core.discovery import (
    DiscoverySearchError,
    GoogleSearchSession,
    browser_headless,
    canonicalize_url,
    classify_source,
    clear_official_domain_cache,
    discover,
    discover_global_official_domains,
    discover_identity_query_with_status,
    discover_with_status,
    is_obvious_non_product_url,
    rank_candidates,
)
from core.identity import resolve_product_identity
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

    def test_lower_google_result_official_exact_product_ranks_first(self) -> None:
        results = [
            ("https://compare.example/questions/X100", "Acme X100 questions and prices"),
            ("https://amazon.com/dp/X100", "Acme X100"),
            ("https://acme.example/product/X100", "Acme X100"),
        ]
        candidates = rank_candidates(
            results,
            "Acme",
            "X100",
            official_domains={"acme.example": "https://acme.example/"},
        )
        self.assertEqual(candidates[0]["url"], "https://acme.example/product/X100")
        self.assertEqual(candidates[0]["authority_status"], "verified")

    def test_official_support_page_is_not_lost_to_product_url_heuristics(self) -> None:
        results = [
            ("https://reviews.example/product/X100", "Acme X100 review"),
            ("https://acme.example/de/supportdetail/X100-01", "Acme X100/01 support"),
        ]
        candidates = rank_candidates(
            results,
            "Acme",
            "X100",
            official_domains={"acme.example": "https://acme.example/"},
        )
        self.assertEqual(
            candidates[0]["url"],
            "https://acme.example/de/supportdetail/X100-01",
        )
        self.assertEqual(candidates[0]["source_type"], "official_document")
        self.assertEqual(candidates[0]["authority_status"], "verified")

    def test_brand_domain_exact_model_boost_does_not_upgrade_authority(self) -> None:
        candidates = rank_candidates([
            ("https://reviews.example/product/X100", "Acme X100 review"),
            ("https://acme-home.example/productservice/X100-01", "Acme X100/01 service"),
        ], "Acme", "X100")
        self.assertEqual(
            candidates[0]["url"],
            "https://acme-home.example/productservice/X100-01",
        )
        self.assertEqual(candidates[0]["source_type"], "other")
        self.assertEqual(candidates[0]["authority_status"], "unknown")

    def test_ranking_is_independent_of_raw_google_order(self) -> None:
        results = [
            ("https://amazon.com/dp/X100", "Acme X100"),
            ("https://acme.example/product/X100", "Acme X100"),
        ]
        options = {
            "official_domains": {"acme.example": "https://acme.example/"},
        }
        forward = rank_candidates(results, "Acme", "X100", **options)
        reverse = rank_candidates(reversed(results), "Acme", "X100", **options)
        self.assertEqual(
            [item["url"] for item in forward],
            [item["url"] for item in reverse],
        )

    def test_candidate_priority_order_is_explicit_in_scores(self) -> None:
        results = [
            ("https://forum.example/questions/X100", "Acme X100 questions"),
            ("https://reference.example/product/X100", "Acme X100"),
            ("https://otto.example/product/X100", "Acme X100"),
            ("https://acme.example/support/X100", "Acme X100 support"),
            ("https://acme.example/product/X100", "Acme X100"),
        ]
        candidates = rank_candidates(
            results,
            "Acme",
            "X100",
            official_domains={"acme.example": "https://acme.example/"},
        )
        self.assertEqual([item["url"] for item in candidates], [
            "https://acme.example/product/X100",
            "https://acme.example/support/X100",
            "https://otto.example/product/X100",
            "https://reference.example/product/X100",
            "https://forum.example/questions/X100",
        ])

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

    def test_brand_like_homepage_is_not_authority_evidence(self) -> None:
        evidence = discover_global_official_domains("Gressel", [
            ("https://gressel.ch/", "GRESSEL AG – Spanntechnik und Werkstück-Automation"),
        ])
        self.assertEqual(evidence, [])
        candidate = rank_candidates([
            ("https://gressel.ch/products/gaf-1825", "Gressel GAF-1825"),
        ], "Gressel", "GAF-1825", official_domains=dict(evidence))[0]
        self.assertEqual(candidate["source_type"], "other")
        self.assertEqual(candidate["authority_status"], "unknown")

    def test_explicit_official_claim_is_authority_evidence(self) -> None:
        evidence = discover_global_official_domains("Acme", [
            ("https://acme.example/", "Acme official website"),
        ])
        self.assertEqual(evidence, [("acme.example", "https://acme.example/")])

    def test_authority_and_model_relevance_are_independent(self) -> None:
        candidates = rank_candidates([
            ("https://acme.example/", "Acme official website"),
            ("https://mvideo.ru/product/X100", "Acme X100"),
        ], "Acme", "X100", official_domain="acme.example",
            authority_evidence_url="https://acme.example/")
        homepage = next(item for item in candidates if item["domain"] == "acme.example")
        retailer = next(item for item in candidates if item["domain"] == "mvideo.ru")
        self.assertEqual(homepage["authority_status"], "verified")
        self.assertEqual(homepage["model_relevance"], "unknown")
        self.assertEqual(retailer["authority_status"], "unknown")
        self.assertEqual(retailer["model_relevance"], "exact_base_model")

    def test_headless_is_true_by_default(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            self.assertTrue(browser_headless())

    def test_canonical_url_keeps_functional_query(self) -> None:
        self.assertEqual(
            canonicalize_url("https://example.com/item?id=7&utm_campaign=x#specs"),
            "https://example.com/item?id=7",
        )

    def test_explicit_targeted_query_reuses_identity_ranking(self) -> None:
        clear_official_domain_cache()
        calls = []

        def searcher(query):
            calls.append(query)
            if query == "Acme official website":
                return [("https://acme.example/", "Acme official website")]
            return [("https://acme.example/product/X100", "Acme X100 net weight")]

        outcome = discover_identity_query_with_status(
            resolve_product_identity("Acme X100"),
            "Acme X100 net weight",
            searcher=searcher,
        )
        self.assertEqual(outcome.queries, ["Acme X100 net weight"])
        self.assertEqual(calls, ["Acme official website", "Acme X100 net weight"])
        self.assertEqual(outcome.candidates[0]["authority_status"], "verified")
        self.assertEqual(outcome.candidates[0]["identity_relation"], "same_base_model")


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

    def test_bot_check_is_structured_blocked_not_empty_success(self) -> None:
        clear_official_domain_cache()

        def blocked_searcher(query):
            raise RuntimeError("Google bot-check blocked the Playwright search.")

        outcome = discover_with_status("Acme", "X100", searcher=blocked_searcher)
        self.assertEqual(outcome.search_status, "blocked")
        self.assertEqual(outcome.candidates, [])
        self.assertEqual(outcome.issues[0].status, "blocked")
        self.assertEqual(outcome.issues[0].query, "Acme official website")
        with self.assertRaises(DiscoverySearchError) as raised:
            discover("Acme", "X100", searcher=blocked_searcher)
        self.assertEqual(raised.exception.outcome.search_status, "blocked")

    def test_results_before_failure_produce_partial_status(self) -> None:
        clear_official_domain_cache()
        calls = 0

        def partial_searcher(query):
            nonlocal calls
            calls += 1
            if calls == 1:
                return [("https://shop.example/product/X100", "Acme X100")]
            raise RuntimeError("Google bot-check blocked the Playwright search.")

        outcome = discover_with_status("Acme", "X100", searcher=partial_searcher)
        self.assertEqual(outcome.search_status, "partial")
        self.assertTrue(outcome.candidates)


if __name__ == "__main__":
    unittest.main()
