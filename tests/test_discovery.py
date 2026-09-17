import unittest
from unittest.mock import MagicMock, patch

from core.discovery import (
    BingSearchProvider,
    DuckDuckGoHtmlSearchProvider,
    DiscoveryRuntimeConfig,
    ProviderParseError,
    ProviderQueryOutcome,
    ProviderTimeoutError,
    ResilientSearchSession,
    SearchResultRecord,
    assess_candidate_relevance,
    _DuckDuckGoHtmlParser,
    _BingParser,
    _clean_bing_result_url,
    _parse_google_browser_items,
    _google_result_layout_ready,
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
from core.budget import WallClockBudget
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

    def test_conflicting_distinctive_code_in_url_overrides_echoed_query_snippet(self) -> None:
        self.assertEqual(
            candidate_model_match(
                "Foodi MAX Dual Zone AF400UK",
                "Ninja Foodi MAX Dual Zone AF400UK product result",
                "/product/ninja-foodi-max-dual-zone-air-fryer-af400me",
            ),
            "mismatch",
        )

    def test_named_family_variant_is_not_the_requested_base_model(self) -> None:
        for title in ("Apple iPhone 15 Pro Max", "Apple iPhone 15 Plus"):
            with self.subTest(title=title):
                self.assertEqual(
                    candidate_model_match("iPhone 15", title, "/phones/iphone-15"),
                    "different_variant",
                )

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

    def test_compound_model_match_recognizes_multiword_identity(self) -> None:
        model = "G12 Pro HHR32A"
        self.assertEqual(
            model_match(model, "Dreame G12 Pro HHR32A wet and dry vacuum"), "exact")
        self.assertEqual(model_match(model, "Dreame HHR32A cordless vacuum"), "exact")
        self.assertEqual(
            model_match(model, "Dreame G12 Pro wet and dry vacuum"), "likely")
        self.assertEqual(model_match(model, "Dreame X30 Ultra"), "unknown")

    def test_compound_model_match_single_word_model_is_unaffected(self) -> None:
        # len(parts) < 2 for a single-word model, so the compound fallback
        # must never fire and existing single-word behavior stays identical.
        self.assertEqual(model_match("PUE611BB5E", "unrelated content"), "unknown")


class DiscoveryTests(unittest.TestCase):
    def test_google_product_subdomains_are_not_blocked_with_search_host(self) -> None:
        self.assertTrue(is_obvious_non_product_url("https://google.com/search?q=x100"))
        self.assertFalse(is_obvious_non_product_url("https://store.google.com/product/x100"))
        self.assertFalse(is_obvious_non_product_url("https://support.google.com/product/x100"))

    def test_exact_brand_root_product_result_can_bootstrap_authority(self) -> None:
        found = discover_global_official_domains(
            "Acme", [],
            product_results=[("https://acme.com/product/X100", "Acme X100")],
            model="X100",
        )
        self.assertEqual(found, [("acme.com", "https://acme.com/product/X100")])

    def test_exact_regional_brand_root_product_can_bootstrap_authority(self) -> None:
        found = discover_global_official_domains(
            "Acme", [],
            product_results=[(
                "https://www.acme.co.uk/products/X100",
                "Acme X100 product specifications",
            )],
            model="X100",
        )

        self.assertEqual(
            found,
            [("acme.co.uk", "https://acme.co.uk/products/X100")],
        )

    def test_fuzzy_regional_brand_root_cannot_bootstrap_authority(self) -> None:
        found = discover_global_official_domains(
            "Acme", [],
            product_results=[(
                "https://acme-shop.co.uk/products/X100",
                "Acme X100 product specifications",
            )],
            model="X100",
        )

        self.assertEqual(found, [])

    def test_two_letter_exact_brand_root_requires_exact_product_evidence(self) -> None:
        found = discover_global_official_domains(
            "HP", [],
            product_results=[(
                "https://support.hp.com/document/x360-14-eu0000",
                "HP Spectre x360 14-eu0000 specifications",
            )],
            model="Spectre x360 14-eu0000",
        )

        self.assertEqual(
            found,
            [("hp.com", "https://support.hp.com/document/x360-14-eu0000")],
        )

    def test_specialized_reference_domains_have_existing_rank_three_role(self) -> None:
        self.assertEqual(classify_source("www.gsmarena.com", None), "specialized_reference")
        self.assertEqual(classify_source("manua.ls", None), "specialized_reference")

    def test_snippet_echo_does_not_turn_different_title_into_exact_model(self) -> None:
        row = rank_candidates([
            SearchResultRecord(
                "https://brand.example/products/y200",
                "Brand Y200",
                "Search for Brand X100 specifications",
                "test", "Brand X100", 1,
            ),
        ], "Brand", "X100")[0]
        self.assertNotEqual(row["model_match"], "exact")

    def test_generic_verified_support_result_is_weak_until_content_verification(self) -> None:
        candidate = {
            "url": "https://support.acme.example/111831",
            "title": "Acme Support",
            "snippet": "",
            "model_match": "unknown",
            "authority_status": "verified",
            "source_type": "official_document",
            "discovery_provenance": [{"query": "Acme X100"}],
        }
        relation, reasons = assess_candidate_relevance(candidate, "Acme", "X100")
        self.assertEqual(relation, "weak")
        self.assertIn("content verification", reasons[0])
    def test_duckduckgo_html_parser_normalizes_layouts_and_snippets(self) -> None:
        html = """
        <div class="result results_links">
          <h2 class="result__title">
            <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Facme.example%2Fproduct%2FX100&amp;rut=abc">
              Acme <b>X100</b> product
            </a>
          </h2>
          <a class="result__snippet">Official specifications for Acme X100.</a>
        </div>
        <div>
          <a class="result-link" href="https://manuals.example/acme-x100.pdf">
            Acme X100 manual
          </a>
          <div class="result-snippet">Installation and operating data.</div>
        </div>
        """
        parser = _DuckDuckGoHtmlParser()
        parser.feed(html)

        self.assertEqual(parser.raw_result_count, 2)
        self.assertEqual(len(parser.results), 2)
        self.assertEqual(parser.results[0].url, "https://acme.example/product/X100")
        self.assertEqual(parser.results[0].title, "Acme X100 product")
        self.assertEqual(
            parser.results[0].snippet,
            "Official specifications for Acme X100.",
        )
        self.assertIn("duckduckgo.com/l/", parser.results[0].raw_url)
        self.assertEqual(
            parser.results[0].redirect_url,
            parser.results[0].raw_url,
        )
        self.assertEqual(parser.results[1].parse_confidence, "high")

    def test_duckduckgo_html_parser_counts_unparseable_organic_links(self) -> None:
        parser = _DuckDuckGoHtmlParser()
        parser.feed("""
            <a class="result__a" href="://malformed">Broken result</a>
            <a class="result__a" href="https://shop.example/X100">Acme X100</a>
        """)

        self.assertEqual(parser.raw_result_count, 2)
        self.assertEqual(len(parser.results), 1)

    def test_default_session_includes_independent_duckduckgo_html_route(self) -> None:
        session = ResilientSearchSession()

        self.assertEqual(
            [provider.name for provider in session.providers],
            [
                "duckduckgo_html", "naver", "duckduckgo_lite", "bing", "google",
                "direct_domain_probe",
            ],
        )
        html_provider = session.providers[0]
        self.assertIsInstance(html_provider, DuckDuckGoHtmlSearchProvider)

    def test_bing_parser_decodes_redirect_and_ignores_navigation_links(self) -> None:
        encoded = "a1aHR0cHM6Ly9hY21lLmV4YW1wbGUvcHJvZHVjdC9YMTAw"
        parser = _BingParser()
        parser.feed(f"""
            <nav><h2><a href="https://navigation.example/">Navigation</a></h2></nav>
            <li class="b_algo">
              <h2><a href="https://www.bing.com/ck/a?u={encoded}&amp;ntb=1">
                Acme <strong>X100</strong> specifications
              </a></h2>
            </li>
        """)

        self.assertEqual(parser.raw_result_count, 1)
        self.assertEqual(len(parser.results), 1)
        self.assertEqual(parser.results[0].url, "https://acme.example/product/X100")
        self.assertEqual(parser.results[0].title, "Acme X100 specifications")
        self.assertEqual(parser.results[0].provider, "bing")
        self.assertIn("bing.com/ck/a", parser.results[0].redirect_url)

    def test_bing_direct_result_url_is_canonicalized(self) -> None:
        self.assertEqual(
            _clean_bing_result_url("https://www.acme.example/X100/?utm_source=bing"),
            "https://acme.example/X100",
        )

    def test_bing_provider_uses_bounded_requests_transport(self) -> None:
        response = MagicMock()
        response.text = """
            <li class="b_algo"><h2>
              <a href="https://acme.example/product/X100">Acme X100</a>
            </h2></li>
        """
        response.raise_for_status.return_value = None
        provider = BingSearchProvider("US")

        with patch("core.discovery.requests.get", return_value=response) as request:
            results = provider.search_with_timeout("Acme X100", 3.5)

        self.assertEqual([item.url for item in results], ["https://acme.example/product/X100"])
        self.assertEqual(provider.last_raw_result_count, 1)
        self.assertEqual(request.call_args.kwargs["timeout"], 3.5)
        self.assertIn("mkt=en-US", request.call_args.args[0])

    def test_duckduckgo_html_provider_uses_requests_transport(self) -> None:
        response = MagicMock()
        response.text = """
            <a class="result__a" href="https://shop.example/product/X100">
                Acme X100
            </a>
            <div class="result__snippet">Product specifications.</div>
        """
        response.raise_for_status.return_value = None
        provider = DuckDuckGoHtmlSearchProvider()

        with patch("core.discovery._DUCKDUCKGO_LAST_REQUEST_AT", 0.0), patch(
            "core.discovery.requests.get", return_value=response,
        ) as request, patch(
            "core.discovery.urlopen", side_effect=AssertionError("urllib route used"),
        ):
            results = provider.search("Acme X100")

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].url, "https://shop.example/product/X100")
        self.assertEqual(provider.last_raw_result_count, 1)
        request.assert_called_once()

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

    def test_negative_official_domain_result_is_cached(self) -> None:
        clear_official_domain_cache()
        calls = []

        def searcher(query):
            calls.append(query)
            if "official" in query:
                return []
            return [("https://shop.example/product/X100", "Acme X100")]

        discover("Acme", "X100", searcher=searcher)
        calls.clear()
        discover_identity_query_with_status(
            resolve_product_identity("Acme X100"),
            "Acme X100 net weight",
            searcher=searcher,
        )

        self.assertEqual(calls, ["Acme X100 net weight"])

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

    def test_exact_brand_root_and_separate_exact_product_prove_official_domain(self) -> None:
        evidence = discover_global_official_domains(
            "Acme",
            [("https://support.acme.com/", "Acme support")],
            product_results=[
                ("https://www.acme.com/products/X100/specs", "Acme X100 specifications"),
            ],
            model="X100",
        )
        self.assertEqual(evidence, [("acme.com", "https://support.acme.com/")])

    def test_brand_root_fallback_rejects_lookalikes_and_uncorroborated_homonyms(self) -> None:
        product = [("https://acme-support.example/X100", "Acme X100")]
        self.assertEqual(discover_global_official_domains(
            "Acme",
            [("https://acme-support.example/", "Acme")],
            product_results=product,
            model="X100",
        ), [])
        self.assertEqual(discover_global_official_domains(
            "Acme",
            [("https://acme.evil.example/", "Acme")],
            product_results=[("https://acme.evil.example/X100", "Acme X100")],
            model="X100",
        ), [])
        self.assertEqual(discover_global_official_domains(
            "Acme",
            [("https://acme.com/", "Acme industrial systems")],
            product_results=[("https://acme.com/about", "Acme industrial systems")],
            model="X100",
        ), [])

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

    def test_press_and_refurbished_results_do_not_enter_product_candidates(self) -> None:
        candidates = rank_candidates([
            ("https://acme.example/newsroom/launch/X100", "Acme launches X100"),
            ("https://acme.example/shop/refurbished-X100", "Refurbished Acme X100"),
            ("https://acme.example/shop/X100-%EC%BC%80%EC%9D%B4%EC%8A%A4", "Acme X100"),
            ("https://acme.example/product/X100", "Acme X100 specifications"),
        ], "Acme", "X100")
        accepted, rejected = [], []
        for item in candidates:
            relation, _ = assess_candidate_relevance(item, "Acme", "X100")
            (rejected if relation == "reject" else accepted).append(item["url"])

        self.assertEqual(accepted, ["https://acme.example/product/X100"])
        self.assertNotIn("https://acme.example/newsroom/launch/X100", [item["url"] for item in candidates])
        self.assertIn("https://acme.example/shop/refurbished-X100", rejected)
        self.assertIn("https://acme.example/shop/X100-%EC%BC%80%EC%9D%B4%EC%8A%A4", rejected)

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

    def test_person_name_collision_is_rejected_before_fetch_selection(self) -> None:
        clear_official_domain_cache()

        def searcher(_query):
            return [
                (
                    "https://sports.example/profile/julian-gressel",
                    "Julian Gressel football player profile and highlights",
                ),
                (
                    "https://appliances.example/product/gaf-1825",
                    "Gressel GAF-1825 air fryer",
                ),
            ]

        outcome = discover_with_status(
            "Gressel", "GAF-1825", searcher=searcher,
        )

        self.assertEqual(
            [item["url"] for item in outcome.candidates],
            ["https://appliances.example/product/gaf-1825"],
        )
        rejected = outcome.rejected_candidates[0]
        self.assertEqual(rejected["relevance_relation"], "reject")
        self.assertIn("person, sports", rejected["relevance_reasons"][0])

    def test_same_brand_wrong_models_are_rejected(self) -> None:
        clear_official_domain_cache()

        def searcher(_query):
            return [
                ("https://brand.example/products/x30-ultra", "Dreame X30 Ultra"),
                ("https://brand.example/products/l20-ultra", "Dreame L20 Ultra"),
                (
                    "https://shop.example/products/g12-pro-hhr32a",
                    "Dreame G12 Pro HHR32A wet dry vacuum",
                ),
            ]

        outcome = discover_with_status(
            "Dreame", "G12 Pro HHR32A", searcher=searcher,
        )

        self.assertEqual(len(outcome.candidates), 1)
        self.assertEqual(outcome.candidates[0]["relevance_relation"], "exact")
        self.assertEqual(len(outcome.rejected_candidates), 2)
        self.assertTrue(all(
            "Brand-only evidence is insufficient" in item["relevance_reasons"][0]
            for item in outcome.rejected_candidates
        ))

    def test_complete_spaced_model_phrase_overrides_token_mismatch_heuristic(self) -> None:
        clear_official_domain_cache()
        outcome = discover_with_status(
            "Janome",
            "Sakura 95",
            searcher=lambda _query: [(
                "https://shop.example/janome-sakura-95",
                "Janome Sakura 95 sewing machine",
            )],
        )

        self.assertEqual(len(outcome.candidates), 1)
        self.assertEqual(outcome.candidates[0]["relevance_relation"], "exact")
        self.assertEqual(outcome.rejected_candidates, [])

    def test_compound_commercial_model_and_mpn_match_across_intervening_words(self) -> None:
        clear_official_domain_cache()
        outcome = discover_with_status(
            "Dreame",
            "G12 Pro HHR32A",
            searcher=lambda _query: [
                (
                    "https://shop.example/dreame-g12-pro-wet-dry-hhr32a",
                    "Dreame G12 Pro Wet & Dry (HHR32A)",
                ),
                (
                    "https://brand.example/products/dreame-x30-ultra",
                    "Dreame X30 Ultra",
                ),
            ],
        )

        self.assertEqual(len(outcome.candidates), 1)
        self.assertEqual(outcome.candidates[0]["relevance_relation"], "exact")
        self.assertEqual(len(outcome.rejected_candidates), 1)

    def test_partial_compound_commercial_model_is_weak_not_unrelated(self) -> None:
        clear_official_domain_cache()
        outcome = discover_with_status(
            "Dreame",
            "G12 Pro HHR32A",
            searcher=lambda _query: [(
                "https://brand.example/products/dreame-g12-pro",
                "Dreame G12 Pro wet dry vacuum",
            )],
        )

        self.assertEqual(outcome.candidates[0]["relevance_relation"], "weak")

    def test_compound_model_exact_candidate_outranks_same_brand_wrong_model(self) -> None:
        candidates = rank_candidates([
            (
                "https://shop.example/dreame-g12-pro-wet-dry-hhr32a",
                "Dreame G12 Pro Wet & Dry (HHR32A)",
            ),
            ("https://brand.example/products/dreame-x30-ultra", "Dreame X30 Ultra"),
        ], "Dreame", "G12 Pro HHR32A")
        exact = next(item for item in candidates if "hhr32a" in item["url"])
        wrong = next(item for item in candidates if "x30-ultra" in item["url"])
        self.assertEqual(exact["model_match"], "exact")
        self.assertGreater(exact["score"], wrong["score"])
        self.assertGreater(exact["score"], 0)

    def test_article_mpn_exact_candidate_outranks_same_brand_wrong_model(self) -> None:
        candidates = rank_candidates([
            ("https://shop.example/dreame-hhr32a-wet-dry", "Dreame HHR32A Wet & Dry Vacuum"),
            ("https://brand.example/products/dreame-x30-ultra", "Dreame X30 Ultra"),
        ], "Dreame", "G12 Pro HHR32A", article="HHR32A")
        exact = next(item for item in candidates if "hhr32a" in item["url"])
        wrong = next(item for item in candidates if "x30-ultra" in item["url"])
        self.assertGreater(exact["score"], wrong["score"])
        self.assertGreater(exact["score"], 0)

    def test_compound_model_scoring_does_not_promote_authority_or_source_type(self) -> None:
        row = rank_candidates([
            ("https://example.test/dreame-g12-pro-hhr32a", "Dreame G12 Pro HHR32A"),
        ], "Dreame", "G12 Pro HHR32A")[0]
        self.assertEqual(row["model_match"], "exact")
        self.assertEqual(row["source_type"], "other")
        self.assertEqual(row["authority_status"], "unknown")

    def test_compound_model_exact_candidate_ranks_first_among_partial_noise(self) -> None:
        clear_official_domain_cache()
        partial_matches = [
            (f"https://retailer{index}.example/dreame-g12-pro", "Dreame G12 Pro wet dry vacuum")
            for index in range(6)
        ]
        results = partial_matches + [(
            "https://shop.example/dreame-g12-pro-wet-dry-hhr32a",
            "Dreame G12 Pro Wet & Dry (HHR32A)",
        )]
        outcome = discover_with_status(
            "Dreame", "G12 Pro HHR32A", searcher=lambda _query: results,
        )
        self.assertEqual(
            outcome.candidates[0]["url"],
            "https://shop.example/dreame-g12-pro-wet-dry-hhr32a",
        )
        self.assertEqual(outcome.candidates[0]["model_match"], "exact")

    def test_exact_model_outranks_and_excludes_brand_only_result(self) -> None:
        clear_official_domain_cache()

        def searcher(_query):
            return [
                ("https://acme.example/products", "Acme product catalog"),
                ("https://retailer.example/item/x100", "Acme X100 specifications"),
            ]

        outcome = discover_with_status("Acme", "X100", searcher=searcher)

        self.assertEqual(len(outcome.candidates), 1)
        self.assertEqual(
            outcome.candidates[0]["url"],
            "https://retailer.example/item/x100",
        )
        self.assertEqual(outcome.candidates[0]["relevance_relation"], "exact")
        self.assertEqual(outcome.rejected_candidates[0]["relevance_relation"], "reject")

    def test_accessory_page_with_exact_model_is_rejected(self) -> None:
        clear_official_domain_cache()

        def searcher(_query):
            return [
                (
                    "https://shop.example/cases/honor-x8d-wallet-cover",
                    "Wallet case and protective cover for HONOR X8d",
                ),
                (
                    "https://honor.example/phones/honor-x8d",
                    "HONOR X8d smartphone",
                ),
            ]

        outcome = discover_with_status("HONOR", "X8d", searcher=searcher)

        self.assertEqual(len(outcome.candidates), 1)
        self.assertEqual(len(outcome.rejected_candidates), 1)
        self.assertIn("accessory", outcome.rejected_candidates[0]["relevance_reasons"][0])

    def test_exact_model_on_generic_toplist_is_weak(self) -> None:
        candidates = rank_candidates(
            [(
                "https://shop.example/producttype/toplist/brand/stovetops",
                "Bosch PUE611BB5E and other induction hobs",
            )],
            "Bosch",
            "PUE611BB5E",
        )
        outcome = discover_with_status(
            "Bosch", "PUE611BB5E", searcher=lambda _query: [
                (candidate["url"], candidate["title"]) for candidate in candidates
            ],
        )

        self.assertEqual(outcome.candidates[0]["relevance_relation"], "weak")


class SearchFallbackTests(unittest.TestCase):
    class Provider:
        def __init__(self, name, response):
            self.name = name
            self.response = response
            self.calls = []

        def search(self, query):
            self.calls.append(query)
            if isinstance(self.response, Exception):
                raise self.response
            return list(self.response)

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
            fallback.assert_called_once()
            self.assertEqual(fallback.call_args.args, ("model",))
            self.assertIn("deadline", fallback.call_args.kwargs)

    def test_consent_page_uses_playwright(self) -> None:
        session = GoogleSearchSession()
        misleading = [("https://example.com/product/model", "Model")]
        with patch(
            "core.discovery._http_google_search_for_market",
            return_value=(misleading, "Before you continue to Google"),
        ), patch.object(session, "_playwright_search", return_value=[]) as fallback:
            session.search("model")
            fallback.assert_called_once()
            self.assertEqual(fallback.call_args.args, ("model",))
            self.assertIn("deadline", fallback.call_args.kwargs)

    def test_google_transient_resources_are_closed_and_reset(self) -> None:
        session = GoogleSearchSession()
        context = MagicMock()
        browser = MagicMock()
        playwright = MagicMock()
        session._context = context
        session._browser = browser
        session._playwright = playwright
        session._page = object()

        session.release_transient_resources()

        context.close.assert_called_once_with()
        browser.close.assert_called_once_with()
        playwright.stop.assert_called_once_with()
        self.assertIsNone(session._page)
        self.assertIsNone(session._context)
        self.assertIsNone(session._browser)
        self.assertIsNone(session._playwright)

    def test_google_cleanup_finishes_remaining_resources_after_close_error(self) -> None:
        session = GoogleSearchSession()
        context = MagicMock()
        context.close.side_effect = RuntimeError("context close failed")
        browser = MagicMock()
        playwright = MagicMock()
        session._context = context
        session._browser = browser
        session._playwright = playwright
        session._page = object()

        with self.assertRaisesRegex(RuntimeError, "context close failed"):
            session.release_transient_resources()

        browser.close.assert_called_once_with()
        playwright.stop.assert_called_once_with()
        self.assertIsNone(session._page)
        self.assertIsNone(session._context)
        self.assertIsNone(session._browser)
        self.assertIsNone(session._playwright)

    def test_resilient_session_releases_managed_provider_resources(self) -> None:
        provider = self.Provider("primary", [])
        provider.release_transient_resources = MagicMock()
        session = ResilientSearchSession(providers=(provider,))

        session.release_transient_resources()

        provider.release_transient_resources.assert_called_once_with()

    def test_bot_check_is_structured_blocked_not_empty_success(self) -> None:
        clear_official_domain_cache()

        def blocked_searcher(query):
            raise RuntimeError("Google bot-check blocked the Playwright search.")

        outcome = discover_with_status("Acme", "X100", searcher=blocked_searcher)
        self.assertEqual(outcome.search_status, "blocked")
        self.assertEqual(outcome.candidates, [])
        self.assertEqual(outcome.issues[0].status, "blocked")
        self.assertEqual(outcome.issues[0].query, "Acme X100")
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

    def test_blocked_official_bootstrap_does_not_skip_exact_model_queries(self) -> None:
        clear_official_domain_cache()
        calls = []

        def searcher(query):
            calls.append(query)
            if query == "Acme official website":
                raise RuntimeError("Google bot-check blocked the Playwright search.")
            return [("https://shop.example/product/X100", "Acme X100")]

        outcome = discover_with_status("Acme", "X100", searcher=searcher)

        self.assertIn("Acme X100", calls)
        self.assertIn('"X100" Acme specifications', calls)
        self.assertTrue(outcome.candidates)
        self.assertEqual(outcome.search_status, "partial")
        self.assertEqual(outcome.issues[0].query, "Acme official website")

    def test_exact_identity_query_precedes_authority_bootstrap(self) -> None:
        clear_official_domain_cache()
        calls = []

        def searcher(query):
            calls.append(query)
            if query == "Acme official website":
                raise RuntimeError("provider bot-check blocked")
            return [("https://shop.example/product/X100", "Acme X100")]

        outcome = discover_with_status("Acme", "X100", searcher=searcher)

        self.assertLess(calls.index("Acme X100"), calls.index("Acme official website"))
        self.assertTrue(outcome.candidates)
        self.assertEqual(outcome.search_status, "partial")

    def test_primary_blocked_uses_fallback_and_preserves_attempts(self) -> None:
        primary = self.Provider("primary", RuntimeError("provider bot-check blocked"))
        fallback = self.Provider(
            "fallback",
            [("https://shop.example/product/X100", "Acme X100")],
        )
        session = ResilientSearchSession(providers=(primary, fallback))

        outcome = session.search_with_status("Acme X100")

        self.assertIsInstance(outcome, ProviderQueryOutcome)
        self.assertEqual([item.status for item in outcome.attempts], ["blocked", "success"])
        self.assertEqual([item.provider for item in outcome.attempts], ["primary", "fallback"])
        self.assertFalse(outcome.attempts[0].is_fallback)
        self.assertTrue(outcome.attempts[1].is_fallback)
        self.assertEqual(outcome.attempts[1].result_count, 1)

    def test_provider_attempt_reports_raw_parsed_deduped_and_transport_counts(self) -> None:
        class InstrumentedProvider:
            name = "instrumented"
            last_raw_result_count = 3
            last_transport = "http"

            def search(self, query):
                return [
                    ("https://shop.example/product/X100", "Acme X100"),
                    ("https://shop.example/product/X100?utm_source=duplicate", "Duplicate"),
                ]

        outcome = ResilientSearchSession(
            providers=(InstrumentedProvider(),),
        ).search_with_status("Acme X100")

        attempt = outcome.attempts[0]
        self.assertEqual(attempt.raw_result_count, 3)
        self.assertEqual(attempt.parsed_result_count, 2)
        self.assertEqual(attempt.deduped_result_count, 1)
        self.assertEqual(attempt.transport, "http")

    def test_primary_error_uses_fallback(self) -> None:
        primary = self.Provider("primary", RuntimeError("transport unavailable"))
        fallback = self.Provider(
            "fallback",
            [("https://shop.example/product/X100", "Acme X100")],
        )
        outcome = ResilientSearchSession(
            providers=(primary, fallback),
        ).search_with_status("Acme X100")

        self.assertEqual([item.status for item in outcome.attempts], ["error", "success"])
        self.assertEqual(fallback.calls, ["Acme X100"])

    def test_second_fallback_runs_when_first_fallback_is_blocked(self) -> None:
        primary = self.Provider("primary", RuntimeError("provider blocked"))
        first_fallback = self.Provider("fallback_one", RuntimeError("captcha blocked"))
        second_fallback = self.Provider(
            "fallback_two",
            [("https://shop.example/product/X100", "Acme X100")],
        )
        outcome = ResilientSearchSession(
            providers=(primary, first_fallback, second_fallback),
        ).search_with_status("Acme X100")

        self.assertEqual(
            [item.status for item in outcome.attempts],
            ["blocked", "blocked", "success"],
        )
        self.assertEqual(
            [item.provider for item in outcome.attempts],
            ["primary", "fallback_one", "fallback_two"],
        )

    def test_blocked_provider_is_not_retried_within_one_session(self) -> None:
        primary = self.Provider("primary", RuntimeError("provider blocked"))
        fallback = self.Provider(
            "fallback",
            [("https://shop.example/product/X100", "Acme X100")],
        )
        session = ResilientSearchSession(providers=(primary, fallback))

        session.search_with_status("Acme X100")
        second = session.search_with_status("Acme X100 specifications")

        self.assertEqual(primary.calls, ["Acme X100"])
        self.assertEqual([item.provider for item in second.attempts], ["primary", "fallback"])
        self.assertEqual(second.attempts[0].status, "circuit_open")
        self.assertTrue(second.attempts[0].circuit_open)
        self.assertTrue(second.attempts[1].is_fallback)

    def test_primary_success_does_not_call_fallback(self) -> None:
        primary = self.Provider(
            "primary",
            [("https://shop.example/product/X100", "Acme X100")],
        )
        fallback = self.Provider("fallback", RuntimeError("must not run"))
        outcome = ResilientSearchSession(
            providers=(primary, fallback),
        ).search_with_status("Acme X100")

        self.assertEqual(len(outcome.attempts), 1)
        self.assertEqual(outcome.attempts[0].status, "success")
        self.assertEqual(fallback.calls, [])

    def test_primary_empty_result_is_classified_and_falls_through(self) -> None:
        primary = self.Provider("primary", [])
        fallback = self.Provider(
            "fallback",
            [("https://shop.example/product/X100", "Acme X100")],
        )
        outcome = ResilientSearchSession(
            providers=(primary, fallback),
        ).search_with_status("Acme X100")

        self.assertEqual(len(outcome.results), 1)
        self.assertEqual(
            [item.status for item in outcome.attempts],
            ["empty", "success"],
        )
        self.assertEqual(fallback.calls, ["Acme X100"])

    def test_low_value_nonempty_result_falls_through_without_opening_circuit(self) -> None:
        class QueryAwareProvider(self.Provider):
            def search(self, query):
                self.calls.append(query)
                if "Y200" in query:
                    return [("https://acme.example/Y200", "Acme Y200")]
                return [("https://unrelated.example/home", "Unrelated home page")]

        primary = QueryAwareProvider("primary", [])
        primary.quality_gate = True
        fallback = self.Provider(
            "fallback", [("https://acme.example/X100", "Acme X100")],
        )
        fallback.quality_gate = True
        session = ResilientSearchSession(providers=(primary, fallback))

        first = session.search_with_status("Acme X100 specifications")
        second = session.search_with_status("Acme Y200 specifications")

        self.assertEqual([item.status for item in first.attempts], ["low_value", "success"])
        self.assertEqual(first.attempts[0].result_count, 1)
        self.assertIn("identity signal", first.attempts[0].message)
        self.assertEqual([item.status for item in second.attempts], ["success"])
        self.assertEqual(len(primary.calls), 2)

    def test_query_quality_requires_distinctive_numeric_model_token(self) -> None:
        primary = self.Provider(
            "primary", [("https://samsung.example/phones", "Samsung Galaxy phones")],
        )
        primary.quality_gate = True
        fallback = self.Provider(
            "fallback",
            [("https://samsung.example/galaxy-z-flip6", "Samsung Galaxy Z Flip6")],
        )
        fallback.quality_gate = True

        outcome = ResilientSearchSession(
            providers=(primary, fallback),
        ).search_with_status("Samsung Galaxy Z Flip6")

        self.assertEqual([item.status for item in outcome.attempts], ["low_value", "success"])

    def test_provider_timeout_opens_request_local_circuit_and_fallback_runs(self) -> None:
        class TimedOutProvider(self.Provider):
            def search_with_timeout(self, query, timeout_seconds):
                self.calls.append((query, timeout_seconds))
                raise ProviderTimeoutError("provider deadline exhausted")

        primary = TimedOutProvider("primary", [])
        fallback = self.Provider(
            "fallback", [("https://shop.example/product/X100", "Acme X100")],
        )
        session = ResilientSearchSession(
            providers=(primary, fallback),
            config=DiscoveryRuntimeConfig(
                provider_timeouts={"primary": 0.25, "fallback": 1.0},
            ),
        )

        first = session.search_with_status("Acme X100")
        second = session.search_with_status("Acme X100 specifications")

        timeout = first.attempts[0]
        self.assertEqual(timeout.status, "timeout")
        self.assertTrue(timeout.timed_out)
        self.assertEqual(timeout.timeout_seconds, 0.25)
        self.assertEqual(timeout.exception_class, "ProviderTimeoutError")
        self.assertEqual(second.attempts[0].status, "circuit_open")
        self.assertEqual(len(primary.calls), 1)

    def test_attempt_telemetry_records_duration_and_block_flags(self) -> None:
        clock_values = iter((10.0, 10.75, 10.75, 11.0))
        primary = self.Provider("primary", RuntimeError("captcha blocked"))
        fallback = self.Provider(
            "fallback", [("https://shop.example/product/X100", "Acme X100")],
        )
        outcome = ResilientSearchSession(
            providers=(primary, fallback),
            clock=lambda: next(clock_values),
        ).search_with_status("Acme X100")

        blocked, success = outcome.attempts
        self.assertEqual(blocked.duration_seconds, 0.75)
        self.assertTrue(blocked.blocked)
        self.assertFalse(blocked.timed_out)
        self.assertEqual(blocked.exception_class, "RuntimeError")
        self.assertEqual(success.duration_seconds, 0.25)

    def test_parse_failure_is_explicit_and_falls_through(self) -> None:
        primary = self.Provider("primary", ProviderParseError("SERP layout changed"))
        fallback = self.Provider(
            "fallback", [("https://shop.example/product/X100", "Acme X100")],
        )

        outcome = ResilientSearchSession(
            providers=(primary, fallback),
        ).search_with_status("Acme X100")

        failed = outcome.attempts[0]
        self.assertEqual(failed.status, "parse_error")
        self.assertTrue(failed.parse_failure)
        self.assertEqual(failed.exception_class, "ProviderParseError")
        self.assertEqual(outcome.attempts[1].status, "success")

    def test_global_budget_caps_provider_deadline_and_prevents_new_fallback(self) -> None:
        class Clock:
            now = 0.0

            def __call__(self):
                return self.now

        clock = Clock()

        class SlowProvider(self.Provider):
            def search_with_timeout(self, query, timeout_seconds):
                self.calls.append((query, timeout_seconds))
                clock.now += 0.6
                return [("https://shop.example/product/X100", "Acme X100")]

        primary = SlowProvider("primary", [])
        fallback = self.Provider("fallback", RuntimeError("must not run"))
        budget = WallClockBudget(0.5, clock)
        outcome = ResilientSearchSession(
            providers=(primary, fallback),
            config=DiscoveryRuntimeConfig(
                provider_timeouts={"primary": 5.0, "fallback": 5.0},
            ),
            clock=clock,
            budget=budget,
        ).search_with_status("Acme X100")

        self.assertEqual(primary.calls[0][1], 0.5)
        self.assertEqual(fallback.calls, [])
        self.assertEqual([item.status for item in outcome.attempts], ["timeout", "timeout"])
        self.assertTrue(outcome.attempts[-1].budget_exhausted)
        self.assertEqual(budget.exhausted_stage, "discovery")

    def test_slow_but_successful_provider_cannot_monopolize_workflow_budget(self) -> None:
        # Stage 18.7: a provider that keeps succeeding, just slowly, must not
        # be able to consume the whole shared workflow budget by winning the
        # fallback race on every single query. Once its cumulative elapsed
        # time reaches its fair share of the total budget, later queries
        # must skip straight to the next provider even though it is not
        # blocked, timed out, or circuit-open.
        class Clock:
            now = 0.0

            def __call__(self):
                return self.now

        clock = Clock()

        class SlowProvider(self.Provider):
            def search_with_timeout(self, query, timeout_seconds):
                self.calls.append((query, timeout_seconds))
                clock.now += 20.0
                return [("https://shop.example/product/X100", "Acme X100")]

        class FastProvider(self.Provider):
            def search_with_timeout(self, query, timeout_seconds):
                self.calls.append((query, timeout_seconds))
                clock.now += 0.1
                return [("https://shop.example/product/X100", "Acme X100")]

        primary = SlowProvider("primary", [])
        fallback = FastProvider("fallback", [])
        budget = WallClockBudget(90.0, clock)
        session = ResilientSearchSession(
            providers=(primary, fallback),
            config=DiscoveryRuntimeConfig(
                provider_timeouts={"primary": 25.0, "fallback": 8.0},
                provider_time_share=0.5,
            ),
            clock=clock,
            budget=budget,
        )

        outcomes = [session.search_with_status(f"query {i}") for i in range(4)]

        # The slow provider is used while its cumulative time is under its
        # 45s fair share (0.5 * 90s), then yields to the fast provider.
        self.assertEqual(len(primary.calls), 3)
        self.assertEqual(len(fallback.calls), 1)
        last_attempts = outcomes[-1].attempts
        self.assertEqual(last_attempts[0].status, "capped")
        self.assertTrue(last_attempts[0].provider_time_capped)
        self.assertEqual(last_attempts[1].status, "success")
        self.assertEqual(last_attempts[1].provider, "fallback")
        # The circuit breaker is a distinct mechanism: the slow provider was
        # never blocked/timed out/errored, so it must not be circuit-open.
        self.assertFalse(any(item.circuit_open for item in last_attempts))
        # Budget still has time remaining; capping freed it for later stages
        # (fetch) instead of letting one provider spend it all.
        self.assertGreater(budget.remaining_seconds, 0.0)

    def test_fast_provider_is_never_time_capped(self) -> None:
        # Regression guard: a provider answering quickly on every query must
        # never hit the fairness cap, no matter how many queries run.
        class Clock:
            now = 0.0

            def __call__(self):
                return self.now

        clock = Clock()

        class FastProvider(self.Provider):
            def search_with_timeout(self, query, timeout_seconds):
                self.calls.append((query, timeout_seconds))
                clock.now += 0.2
                return [("https://shop.example/product/X100", "Acme X100")]

        primary = FastProvider("primary", [])
        budget = WallClockBudget(90.0, clock)
        session = ResilientSearchSession(
            providers=(primary,),
            config=DiscoveryRuntimeConfig(provider_timeouts={"primary": 25.0}),
            clock=clock,
            budget=budget,
        )

        for i in range(20):
            outcome = session.search_with_status(f"query {i}")
            self.assertEqual(outcome.attempts[0].status, "success")

        self.assertEqual(len(primary.calls), 20)

    def test_generic_browser_items_handle_redirects_layouts_and_duplicates(self) -> None:
        items = [
            {
                "href": (
                    "https://www.google.com/url?sa=t&url="
                    "https%3A%2F%2Facme.example%2Fproduct%2FX100%3Futm_source%3Dgoogle"
                ),
                "title": "Acme X100",
                "text": "Acme X100 Official specifications and dimensions",
            },
            {
                "href": "",
                "dataHref": "https://acme.example/product/X100",
                "title": "Acme X100 duplicate",
                "text": "Duplicate layout",
            },
            {"href": "://malformed", "title": "Broken", "text": "Broken"},
        ]

        results = _parse_google_browser_items(items)

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].url, "https://acme.example/product/X100")
        self.assertEqual(results[0].title, "Acme X100")
        self.assertIn("Official specifications", results[0].snippet)
        self.assertEqual(results[0].parse_confidence, "high")

    def test_browser_result_readiness_accepts_heading_outside_anchor(self) -> None:
        page = MagicMock()
        page.locator.side_effect = lambda selector: MagicMock(
            count=lambda: 1 if selector == "h3" else 0,
        )

        self.assertTrue(_google_result_layout_ready(page))

    def test_candidate_contract_merges_cross_provider_provenance(self) -> None:
        url = "https://shop.example/product/X100"
        candidates = rank_candidates([
            SearchResultRecord(
                url, "Acme X100", "First snippet", "google", "Acme X100", 2,
                raw_url=f"{url}?utm_source=google",
            ),
            SearchResultRecord(
                url, "Acme X100", "Second snippet", "naver", '"X100" Acme', 1,
                raw_url=url,
            ),
        ], "Acme", "X100")

        self.assertEqual(len(candidates), 1)
        item = candidates[0]
        self.assertEqual(item["url"], url)
        self.assertEqual(
            {entry["provider"] for entry in item["discovery_provenance"]},
            {"google", "naver"},
        )
        self.assertIn(item["parse_confidence"], {"medium", "high"})
        self.assertEqual(item["authority_status"], "unknown")

    def test_fallback_results_use_normal_dedupe_scoring_without_authority_upgrade(self) -> None:
        primary = self.Provider("primary", RuntimeError("provider blocked"))
        fallback = self.Provider("fallback", [
            ("https://amazon.com/dp/X100", "Acme X100"),
            ("https://acme.example/product/X100?utm_source=fallback", "Acme X100"),
            ("https://acme.example/product/X100", "Acme X100 duplicate"),
        ])
        search = ResilientSearchSession(
            providers=(primary, fallback),
        ).search_with_status

        outcome = discover_with_status("Acme", "X100", searcher=search)

        urls = [item["url"] for item in outcome.candidates]
        self.assertEqual(urls.count("https://acme.example/product/X100"), 1)
        self.assertEqual(urls[0], "https://acme.example/product/X100")
        product = outcome.candidates[0]
        self.assertEqual(product["source_type"], "other")
        self.assertEqual(product["authority_status"], "unknown")
        self.assertEqual(product["relevance_relation"], "exact")
        self.assertTrue(any(item.is_fallback for item in outcome.provider_attempts))

    def test_fallback_brand_only_pollution_is_rejected(self) -> None:
        primary = self.Provider("primary", RuntimeError("provider blocked"))
        fallback = self.Provider("fallback", [
            ("https://sports.example/profile/acme", "Acme football player profile"),
            ("https://shop.example/product/X100", "Acme X100"),
        ])
        search = ResilientSearchSession(
            providers=(primary, fallback),
        ).search_with_status

        outcome = discover_with_status("Acme", "X100", searcher=search)

        self.assertEqual(len(outcome.candidates), 1)
        self.assertEqual(len(outcome.rejected_candidates), 1)
        self.assertEqual(outcome.candidates[0]["authority_status"], "unknown")
        self.assertEqual(outcome.rejected_candidates[0]["relevance_relation"], "reject")

    def test_official_claim_from_fallback_provider_seeds_manufacturer_authority(self) -> None:
        # Stage 18.7: official-domain corroboration no longer depends on one
        # specific provider (previously: only a non-fallback/primary success
        # counted). The primary provider is blocked here; a fallback
        # provider alone returns the "official website" evidence, and it
        # meets the same evidence bar (domain/brand consistency + explicit
        # "official" wording + brand name in the title) a primary-provider
        # claim would have to meet, so it seeds authority just the same.
        clear_official_domain_cache()
        primary = self.Provider("primary", RuntimeError("provider blocked"))

        class QueryFallback:
            name = "fallback"

            def search(self, query):
                if query == "Acme official website":
                    return [("https://acme.example/", "Acme official website")]
                return [("https://acme.example/product/X100", "Acme X100")]

        search = ResilientSearchSession(
            providers=(primary, QueryFallback()),
        ).search_with_status

        outcome = discover_with_status("Acme", "X100", searcher=search)
        product = next(
            item for item in outcome.candidates
            if item["url"] == "https://acme.example/product/X100"
        )

        self.assertEqual(product["source_type"], "manufacturer")
        self.assertEqual(product["authority_status"], "verified")
        self.assertEqual(product["authority_evidence_url"], "https://acme.example/")

    def test_fallback_official_claim_without_domain_consistency_stays_unverified(self) -> None:
        # Content evidence, not provider trust, is what must gate
        # verification: a fallback claim naming the right brand wording but
        # pointing at an unrelated domain must still be rejected.
        clear_official_domain_cache()
        primary = self.Provider("primary", RuntimeError("provider blocked"))

        class QueryFallback:
            name = "fallback"

            def search(self, query):
                if query == "Acme official website":
                    return [("https://unrelated-shop.example/", "Acme official website")]
                return [("https://acme.example/product/X100", "Acme X100")]

        search = ResilientSearchSession(
            providers=(primary, QueryFallback()),
        ).search_with_status

        outcome = discover_with_status("Acme", "X100", searcher=search)
        product = next(
            item for item in outcome.candidates
            if item["url"] == "https://acme.example/product/X100"
        )

        self.assertEqual(product["source_type"], "other")
        self.assertEqual(product["authority_status"], "unknown")

    def test_marketplace_domain_official_claim_from_any_provider_is_never_manufacturer(self) -> None:
        # A brand whose name coincides with a marketplace's domain label
        # must never let an "official site" claim - from any provider -
        # promote the marketplace domain itself to manufacturer authority.
        clear_official_domain_cache()
        primary = self.Provider("primary", RuntimeError("provider blocked"))

        class QueryFallback:
            name = "fallback"

            def search(self, query):
                if query == "Ozon official website":
                    return [("https://ozon.ru/", "Ozon official website")]
                return [("https://ozon.ru/product/X100", "Ozon X100")]

        search = ResilientSearchSession(
            providers=(primary, QueryFallback()),
        ).search_with_status

        outcome = discover_with_status("Ozon", "X100", searcher=search)
        product = next(
            item for item in outcome.candidates
            if item["url"] == "https://ozon.ru/product/X100"
        )

        self.assertEqual(product["source_type"], "marketplace")
        self.assertNotEqual(product["authority_status"], "verified")


if __name__ == "__main__":
    unittest.main()
