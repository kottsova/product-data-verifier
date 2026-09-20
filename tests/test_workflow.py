"""Deterministic Stage 8 orchestration tests with no live network access."""

import unittest
from datetime import date
from unittest.mock import MagicMock, patch

from core.budget import BudgetExhaustedError
from core.authority_registry import AuthoritySeed
from core.discovery import DiscoveryOutcome, ProviderAttempt
from core.export import profile_rows, profile_to_dict
from core.extract import RawAttribute
from core.quality import assess_product_quality
from core.targeted_search import TargetedSearchConfig
from core.workflow import (
    ProductWorkflowRequest,
    WorkflowServices,
    _discover_initial_with_released_browser,
    _discover_targeted_with_released_browser,
    _verify_fetched_identity,
    run_product_workflow,
    select_source_candidates,
)


def candidate(
    url,
    *,
    title="Acme product X100",
    source_type="manufacturer",
    authority="verified",
    relation="exact_variant",
    relevance="exact",
    score=100,
):
    return {
        "url": url,
        "domain": url.split("/")[2],
        "title": title,
        "source_type": source_type,
        "authority_status": authority,
        "authority_evidence_url": url.split("/product")[0],
        "authority_reason": "Synthetic workflow fixture.",
        "product_match_evidence": "Synthetic exact-model fixture.",
        "market_scope": "global",
        "model_match": "exact",
        "model_relevance": "exact_base_model",
        "score": score,
        "identity_relation": relation,
        "identity_verification_evidence": ["model=X100"],
        "relevance_relation": relevance,
        "relevance_reasons": ["Synthetic relevance fixture."],
    }


def fixture_authority_seed(brand, host):
    """Test-only audited hosts for synthetic workflow fixtures."""
    if not host.endswith(".example"):
        return None
    return AuthoritySeed(brand, host, "brand_operator", "fixture", f"https://{host}/audit", date.today())


def fetch_result(item, *, status="success"):
    url = item["url"]
    return {
        "source_url": url,
        "final_url": url,
        "status": status,
        "http_status": 200 if status == "success" else 503,
        "fetch_method": "requests",
        "content_type": "text/html",
        "document_type": "html",
        "html": f"<html><body><h1>{item['title']}</h1></body></html>",
        "text": item["title"],
        "content": b"fixture",
        "pdf_text": "",
        "text_status": "available",
        "blocked_reason": None,
        "error": None if status == "success" else "Synthetic fetch failure.",
        "source_type": item["source_type"],
        "authority_status": item["authority_status"],
        "authority_evidence_url": item["authority_evidence_url"],
        "model_relevance": item["model_relevance"],
        "identity_relation": item["identity_relation"],
        "discovery_metadata": dict(item),
    }


def raw(name, value, source, *, source_type="manufacturer", context=None):
    return RawAttribute(
        name=name,
        value=value,
        unit=None,
        source_url=source,
        source_type=source_type,
        evidence=f"{name}: {value}",
        extraction_method="html_table",
        confidence="high",
        raw_value=value,
        attribute_kind="product",
        context=context,
    )


class FixtureServices:
    def __init__(self, initial, attributes, *, targeted=None, failures=()):
        self.initial = list(initial)
        self.attributes = attributes
        self.targeted = targeted or {}
        self.failures = set(failures)
        self.fetch_calls = []
        self.targeted_calls = []

    def discover_initial(self, _identity, _market):
        return DiscoveryOutcome(self.initial, "success", ["initial"], ["initial"], [])

    def discover_targeted(self, _identity, query, _market):
        self.targeted_calls.append(query)
        items = next(
            (values for marker, values in self.targeted.items() if marker in query.casefold()),
            [],
        )
        return DiscoveryOutcome(list(items), "success", [query], [query], [])

    def fetch(self, item):
        self.fetch_calls.append(item["url"])
        return fetch_result(
            item,
            status="error" if item["url"] in self.failures else "success",
        )

    def extract(self, source):
        return list(self.attributes.get(source["source_url"], ()))

    def services(self):
        return WorkflowServices(
            self.discover_initial,
            self.discover_targeted,
            self.fetch,
            self.extract,
        )


class ProductWorkflowTests(unittest.TestCase):
    def setUp(self):
        registry = patch("core.workflow.find_seed", side_effect=fixture_authority_seed)
        registry.start()
        self.addCleanup(registry.stop)

    def test_selection_reserves_verified_exact_official_document(self):
        broad = [
            candidate(
                f"https://acme.example/product/X100-{index}",
                score=200 - index,
            )
            for index in range(3)
        ]
        specification = candidate(
            "https://support.acme.example/specifications/X100",
            source_type="official_document",
            score=100,
        )

        selected = select_source_candidates([*broad, specification], 3)

        self.assertEqual(len(selected), 3)
        self.assertIn(specification, selected)
        self.assertEqual(selected[0], specification)
        self.assertEqual(selected[1:], tuple(broad[:2]))

    def test_selection_limits_weak_multi_product_pages_to_one(self):
        exact = candidate("https://acme.example/product/X100", score=100)
        weak = [
            candidate(
                f"https://acme.example/compare/X100-{index}",
                relevance="weak",
                score=200 - index,
            )
            for index in range(3)
        ]

        selected = select_source_candidates([*weak, exact], 5)

        self.assertEqual(selected[0], exact)
        self.assertEqual(len([item for item in selected if item["relevance_relation"] == "weak"]), 1)

    def test_budget_exhaustion_returns_best_partial_result_without_new_fetch(self):
        class Clock:
            now = 0.0

            def __call__(self):
                return self.now

        clock = Clock()
        url = "https://acme.example/product/X100"
        item = candidate(url)
        fetch_calls = []

        def discover(_identity, _market):
            clock.now = 1.0
            return DiscoveryOutcome([item], "success", ["initial"], ["initial"], [])

        services = WorkflowServices(
            discover_initial=discover,
            fetch=lambda value: fetch_calls.append(value) or fetch_result(value),
            clock=clock,
        )
        result = run_product_workflow(
            ProductWorkflowRequest(
                "Acme X100",
                brand="Acme",
                targeted_search_enabled=False,
                wall_clock_budget_seconds=0.5,
            ),
            services=services,
        )

        self.assertEqual(fetch_calls, [])
        self.assertEqual(len(result.selected_candidates), 1)
        self.assertEqual(result.fetched_sources, ())
        budget = result.final_profile.metadata["wall_clock_budget"]
        self.assertTrue(budget["exhausted"])
        self.assertEqual(budget["exhausted_stage"], "initial_fetch")
        self.assertIn("Insufficient workflow budget", budget["exhaustion_reason"])

    def test_fetch_budget_race_returns_degraded_result_not_workflow_failure(self):
        item = candidate("https://acme.example/product/X100")
        services = WorkflowServices(
            discover_initial=lambda _identity, _market: DiscoveryOutcome(
                [item], "success", ["initial"], ["initial"], [],
            ),
            fetch=lambda _item: (_ for _ in ()).throw(
                BudgetExhaustedError("Insufficient workflow budget to start fetch.")
            ),
        )

        result = run_product_workflow(
            ProductWorkflowRequest(
                "Acme X100", brand="Acme", targeted_search_enabled=False,
            ),
            services=services,
        )

        self.assertEqual(result.fetched_sources, ())
        self.assertEqual(len(result.selected_candidates), 1)
        self.assertEqual(result.quality.status, "insufficient")

    def test_successful_source_is_extracted_before_later_fetches_exhaust_budget(self):
        class Clock:
            now = 0.0

            def __call__(self):
                return self.now

        clock = Clock()
        urls = [
            "https://one.example/product/X100",
            "https://two.example/product/X100",
        ]
        items = [candidate(url, title="Acme Air fryer X100") for url in urls]
        fetch_calls = []
        extract_calls = []

        def fetch(item):
            fetch_calls.append(item["url"])
            clock.now += 0.48
            return fetch_result(item)

        def extract(source):
            extract_calls.append(source["source_url"])
            clock.now += 0.10
            return [raw("Power", "1000 W", source["source_url"])]

        services = WorkflowServices(
            discover_initial=lambda _identity, _market: DiscoveryOutcome(
                items, "success", ["initial"], ["initial"], [],
            ),
            fetch=fetch,
            extract=extract,
            clock=clock,
        )

        result = run_product_workflow(
            ProductWorkflowRequest(
                "Acme X100",
                brand="Acme",
                targeted_search_enabled=False,
                wall_clock_budget_seconds=1.0,
            ),
            services=services,
        )

        self.assertEqual(fetch_calls, [urls[0]])
        self.assertEqual(extract_calls, [urls[0]])
        self.assertEqual([item.name for item in result.raw_attributes], ["Power", "Brand", "Model"])

    def test_relevance_gate_may_select_fewer_sources_and_never_fetches_rejected(self):
        accepted_url = "https://shop.example/product/X100"
        rejected_urls = [
            "https://sports.example/profile/acme",
            "https://shop.example/product/X200",
        ]
        fixtures = FixtureServices(
            [
                candidate(accepted_url),
                candidate(
                    rejected_urls[0], title="Acme football player profile",
                    relevance="reject", relation="unknown", score=200,
                ),
                candidate(
                    rejected_urls[1], title="Acme X200", relevance="reject",
                    relation="different_model", score=190,
                ),
            ],
            {accepted_url: []},
        )

        result = run_product_workflow(
            ProductWorkflowRequest(
                "Acme X100", brand="Acme", max_initial_sources=5,
                targeted_search_enabled=False,
            ),
            services=fixtures.services(),
        )

        self.assertEqual(fixtures.fetch_calls, [accepted_url])
        self.assertEqual(len(result.selected_candidates), 1)

    def test_full_initial_pipeline_builds_final_profile(self):
        url = "https://acme.example/product/X100"
        item = candidate(url, title="Acme Smartphone X100")
        fixtures = FixtureServices([item], {
            url: [
                raw("Brand", "Acme", url),
                raw("Model", "X100", url),
                raw("Battery capacity", "5000 mAh", url),
                raw("Custom airflow mode", "Quiet", url),
            ],
        })
        result = run_product_workflow(
            ProductWorkflowRequest(
                "Acme Smartphone X100",
                brand="Acme",
                targeted_search_enabled=False,
            ),
            services=fixtures.services(),
        )

        self.assertEqual(result.category.category_id, "smartphone")
        self.assertEqual(result.final_profile.by_name["battery_capacity"].status, "Confirmed")
        discovered = result.final_profile.by_name["custom_airflow_mode"]
        self.assertEqual(discovered.status, "Confirmed")
        self.assertTrue(discovered.discovered)
        self.assertFalse(discovered.expected)
        self.assertEqual(result.final_profile.identity.brand, "Acme")
        self.assertEqual(result.final_profile.metadata["workflow_version"], "1.0")
        self.assertEqual(result.quality, assess_product_quality(result.final_profile))

    def test_verified_exact_official_page_supplies_missing_brand_and_model_facts(self):
        url = "https://acme.example/support/X100/specifications"
        item = candidate(url, title="Acme X100 specifications")
        fixtures = FixtureServices([item], {url: []})

        def fetch(candidate_item):
            result = fetch_result(candidate_item)
            result["text"] = "Official technical specifications for Acme X100."
            result["html"] = "<html><body><h1>Official technical specifications for Acme X100.</h1></body></html>"
            return result

        fixtures.fetch = fetch
        result = run_product_workflow(
            ProductWorkflowRequest(
                "Acme X100", brand="Acme", targeted_search_enabled=False,
            ),
            services=fixtures.services(),
        )

        self.assertEqual(
            [item.name for item in result.raw_attributes],
            ["Brand", "Model"],
        )
        self.assertEqual(result.final_profile.by_name["brand"].status, "Confirmed")
        self.assertEqual(result.final_profile.by_name["model"].status, "Confirmed")

    def test_identity_synthesis_rejects_unknown_authority_and_non_exact_page_text(self):
        cases = (
            candidate(
                "https://shop.example/X100",
                source_type="retailer",
                authority="unknown",
            ),
            candidate("https://acme.example/support/X100"),
        )
        page_texts = (
            "Acme X100 product details",
            "Official technical specifications for Acme X100 Pro",
        )
        for item, page_text in zip(cases, page_texts):
            with self.subTest(url=item["url"]):
                fixtures = FixtureServices([item], {item["url"]: []})

                def fetch(candidate_item, text=page_text):
                    result = fetch_result(candidate_item)
                    result["text"] = text
                    result["html"] = f"<html><body>{text}</body></html>"
                    return result

                fixtures.fetch = fetch
                result = run_product_workflow(
                    ProductWorkflowRequest(
                        "Acme X100", brand="Acme", targeted_search_enabled=False,
                    ),
                    services=fixtures.services(),
                )
                self.assertEqual(result.raw_attributes, ())

    def test_identity_synthesis_does_not_overwrite_extracted_model_alias(self):
        url = "https://support.acme.example/specifications/X100"
        item = candidate(url)
        fixtures = FixtureServices([item], {
            url: [raw("Model Number", "X100-US", url)],
        })

        def fetch(candidate_item):
            result = fetch_result(candidate_item)
            result["text"] = "Official technical specifications for Acme X100."
            result["html"] = "<html><body><h1>Official technical specifications for Acme X100.</h1></body></html>"
            return result

        fixtures.fetch = fetch
        result = run_product_workflow(
            ProductWorkflowRequest(
                "Acme X100", brand="Acme", targeted_search_enabled=False,
            ),
            services=fixtures.services(),
        )

        self.assertEqual(
            [(item.name, item.value) for item in result.raw_attributes],
            [("Model Number", "X100-US"), ("Brand", "Acme")],
        )
        self.assertIn(result.quality.status, ("verified", "partial", "insufficient", "conflicted"))

    def test_retailer_authority_is_preserved_and_fact_stays_unresolved(self):
        url = "https://shop.example/product/X100"
        item = candidate(
            url,
            title="Acme Air fryer X100",
            source_type="retailer",
            authority="unknown",
        )
        fixtures = FixtureServices([item], {
            url: [raw("Power", "1800 W", url, source_type="retailer")],
        })
        result = run_product_workflow(
            ProductWorkflowRequest(
                "Acme Air fryer X100",
                brand="Acme",
                targeted_search_enabled=False,
            ),
            services=fixtures.services(),
        )
        power = result.final_profile.by_name["power"]
        self.assertEqual(power.status, "Unresolved")
        self.assertEqual(power.authority_status, "unknown")
        self.assertEqual(power.supporting_sources[0].source_type, "retailer")

    def test_different_official_values_remain_a_visible_conflict(self):
        first = "https://one.example/product/X100"
        second = "https://two.example/product/X100"
        fixtures = FixtureServices(
            [
                candidate(first, title="Acme Air fryer X100", score=100),
                candidate(second, title="Acme Air fryer X100", score=90),
            ],
            {
                first: [raw("Power", "1800 W", first)],
                second: [raw("Power", "2000 W", second)],
            },
        )
        result = run_product_workflow(
            ProductWorkflowRequest(
                "Acme Air fryer X100",
                brand="Acme",
                targeted_search_enabled=False,
            ),
            services=fixtures.services(),
        )
        power = result.final_profile.by_name["power"]
        self.assertEqual(power.status, "Conflict")
        rows = [row for row in profile_rows(result.final_profile) if row["Attribute"] == "power"]
        self.assertEqual({row["Value"] for row in rows}, {"1800 W", "2000 W"})
        self.assertEqual(len(rows), 2)

    def test_targeted_search_output_reaches_validation_and_profile(self):
        initial_url = "https://janome.example/product/Sakura-95"
        reverse_url = "https://janome.example/manual/Sakura-95"
        initial = candidate(initial_url, title="Janome sewing machine Sakura 95")
        reverse = candidate(reverse_url, title="Janome Sakura 95 manual")
        fixtures = FixtureServices(
            [initial],
            {
                initial_url: [
                    raw("Machine type", "Electromechanical", initial_url),
                    raw("Operation count", "15", initial_url),
                ],
                reverse_url: [raw("Reverse", "Yes", reverse_url)],
            },
            targeted={"reverse": [reverse]},
        )
        result = run_product_workflow(
            ProductWorkflowRequest(
                "Janome sewing machine Sakura 95",
                brand="Janome",
                targeted_config=TargetedSearchConfig(
                    max_queries_per_field=1,
                    max_candidates_per_query=1,
                    max_candidates_per_field=1,
                    max_total_queries=20,
                    max_consecutive_zero_candidates=20,
                    max_consecutive_zero_useful_facts=20,
                    max_consecutive_duplicate_domains=20,
                    max_consecutive_no_coverage_gain=20,
                    max_consecutive_no_confirmed_gain=20,
                ),
            ),
            services=fixtures.services(),
        )
        reverse_fact = result.final_profile.by_name["reverse"]
        self.assertEqual(reverse_fact.status, "Confirmed")
        self.assertEqual(reverse_fact.source, reverse_url)
        self.assertEqual(reverse_fact.supporting_sources[0].origin, "targeted_search")
        self.assertGreater(len(fixtures.targeted_calls), 0)

    def test_fetch_cache_is_shared_with_targeted_search(self):
        url = "https://acme.example/product/X100"
        initial_item = candidate(
            url,
            title="Acme Smartphone X100",
            source_type="other",
            authority="unknown",
        )
        targeted_item = candidate(url, title="Acme Smartphone X100")
        fixtures = FixtureServices(
            [initial_item],
            {url: [raw("Brand", "Acme", url)]},
            targeted={"battery capacity": [targeted_item]},
        )
        result = run_product_workflow(
            ProductWorkflowRequest(
                "Acme Smartphone X100",
                brand="Acme",
                targeted_config=TargetedSearchConfig(
                    max_queries_per_field=1,
                    max_candidates_per_query=1,
                    max_candidates_per_field=1,
                    max_total_queries=20,
                    max_consecutive_zero_candidates=20,
                    max_consecutive_zero_useful_facts=20,
                    max_consecutive_duplicate_domains=20,
                    max_consecutive_no_coverage_gain=20,
                    max_consecutive_no_confirmed_gain=20,
                ),
            ),
            services=fixtures.services(),
        )
        self.assertEqual(fixtures.fetch_calls.count(url), 1)
        battery_search = next(
            field for field in result.targeted_search.fields
            if field.gap.canonical_name == "battery_capacity"
        )
        fetched = battery_search.query_results[0].fetched_sources[0]
        self.assertEqual(fetched["authority_status"], "verified")
        self.assertEqual(fetched["source_type"], "manufacturer")

    def test_lower_official_candidate_is_selected_before_source_limit(self):
        urls = [
            "https://one.example/product/X100",
            "https://two.example/product/X100",
        ]
        fixtures = FixtureServices(
            [
                candidate(
                    urls[0], source_type="other", authority="unknown", score=95,
                ),
                candidate(urls[1], score=170),
            ],
            {url: [] for url in urls},
        )
        result = run_product_workflow(
            ProductWorkflowRequest(
                "Acme Smartphone X100",
                brand="Acme",
                max_initial_sources=1,
                targeted_search_enabled=False,
            ),
            services=fixtures.services(),
        )
        self.assertEqual(fixtures.fetch_calls, [urls[1]])
        self.assertEqual(result.selected_candidates[0]["url"], urls[1])
        self.assertEqual(len(result.fetched_sources), 1)

    def test_official_document_and_source_diversity_beat_regional_mirrors(self):
        rows = [
            candidate(
                "https://brand.example/gb/product/X100", score=190,
            ),
            candidate(
                "https://brand.example/de/product/X100", score=180,
            ),
            candidate(
                "https://docs.brand.example/manual/X100.pdf",
                source_type="official_document", score=130,
            ),
            candidate(
                "https://reference.example/specs/X100",
                source_type="specialized_reference", authority="unknown", score=100,
            ),
        ]
        selected = select_source_candidates(rows, 3)
        self.assertEqual(selected[0]["source_type"], "official_document")
        self.assertEqual(
            [item["url"] for item in selected],
            [rows[2]["url"], rows[0]["url"], rows[3]["url"]],
        )

    def test_verified_support_fetch_upgrades_identity_only_from_page_content(self):
        item = candidate(
            "https://support.acme.example/111831",
            title="Acme Support", source_type="official_document",
            relation="unknown", relevance="weak", score=20,
        )
        item["model_match"] = "unknown"
        item["model_relevance"] = "unknown"
        item["relevance_reasons"] = [
            "Verified first-party result from an exact-model query requires content verification."
        ]
        source = fetch_result(item)
        source["text"] = "Acme X100 technical specifications"
        source["html"] = "<h1>Acme X100 technical specifications</h1>"
        identity = run_product_workflow(
            ProductWorkflowRequest(
                "Acme X100", brand="Acme", targeted_search_enabled=False,
            ),
            services=FixtureServices([], {}).services(),
        ).identity

        verified = _verify_fetched_identity(source, item, identity)
        self.assertEqual(verified["identity_relation"], "same_base_model")
        self.assertEqual(verified["model_relevance"], "exact_base_model")
        self.assertTrue(verified["discovery_metadata"]["content_identity_verified"])

        absent = fetch_result(item)
        absent["text"] = "Acme support landing page"
        self.assertEqual(
            _verify_fetched_identity(absent, item, identity)["identity_relation"],
            "unknown",
        )

        incidental = fetch_result(item)
        incidental["text"] = "Compare products including Acme X100"
        incidental["html"] = (
            "<title>All Acme products</title><h1>Latest products</h1>"
            "<p>Compare products including Acme X100</p>"
        )
        self.assertEqual(
            _verify_fetched_identity(incidental, item, identity)["identity_relation"],
            "unknown",
        )

    def test_exact_reference_precedes_extra_verification_fetch_after_official_hit(self):
        exact_official = candidate(
            "https://support.acme.example/manual/X100",
            source_type="official_document", score=100,
        )
        verification = candidate(
            "https://acme.example/products", title="Acme products",
            source_type="manufacturer", relation="unknown", relevance="weak",
            score=150,
        )
        verification["model_match"] = "unknown"
        verification["model_relevance"] = "unknown"
        verification["relevance_reasons"] = [
            "Verified first-party result from an exact-model query requires content verification."
        ]
        reference = candidate(
            "https://reference.example/specs/X100",
            source_type="specialized_reference", authority="unknown", score=80,
        )

        selected = select_source_candidates(
            [verification, exact_official, reference], 3,
        )
        self.assertEqual(
            [item["url"] for item in selected],
            [exact_official["url"], reference["url"], verification["url"]],
        )

    def test_fetch_failure_is_retained_and_not_extracted(self):
        url = "https://broken.example/product/X100"
        fixtures = FixtureServices(
            [candidate(url)],
            {url: [raw("Power", "1000 W", url)]},
            failures=[url],
        )
        result = run_product_workflow(
            ProductWorkflowRequest(
                "Acme Air fryer X100",
                brand="Acme",
                targeted_search_enabled=False,
            ),
            services=fixtures.services(),
        )
        self.assertEqual(len(result.fetch_failures), 1)
        self.assertEqual(result.raw_attributes, ())
        self.assertEqual(result.summary["successful_initial_fetch_count"], 0)

    def test_exact_candidate_title_can_classify_after_blocked_fetch(self):
        url = "https://blocked.example/product/X100"
        fixtures = FixtureServices(
            [candidate(url, title="Acme Air fryer X100")],
            {},
            failures=[url],
        )
        result = run_product_workflow(
            ProductWorkflowRequest(
                "Acme X100",
                brand="Acme",
                targeted_search_enabled=False,
            ),
            services=fixtures.services(),
        )
        self.assertEqual(result.category.category_id, "air_fryer")
        self.assertEqual(result.raw_attributes, ())

    def test_empty_discovery_still_returns_an_unresolved_profile(self):
        fixtures = FixtureServices([], {})
        result = run_product_workflow(
            ProductWorkflowRequest(
                "Acme Smartphone X100",
                brand="Acme",
                targeted_search_enabled=False,
            ),
            services=fixtures.services(),
        )
        self.assertEqual(result.discovery.search_status, "success")
        self.assertEqual(result.fetched_sources, ())
        self.assertTrue(result.final_profile.unresolved)
        self.assertIn("battery_capacity", profile_to_dict(result.final_profile)["unresolved"])

    def test_from_parts_preserves_named_article_as_identity_evidence(self):
        request = ProductWorkflowRequest.from_parts("Acme", "X100", "ABC-12345")
        fixtures = FixtureServices([], {})
        result = run_product_workflow(
            ProductWorkflowRequest(
                request.raw_name,
                brand=request.brand,
                identity_evidence=request.identity_evidence,
                targeted_search_enabled=False,
            ),
            services=fixtures.services(),
        )
        self.assertEqual(result.identity.manufacturer_article, "ABC-12345")

    def test_invalid_request_is_rejected_before_discovery(self):
        with self.assertRaisesRegex(ValueError, "raw_name is required"):
            ProductWorkflowRequest("   ")
        with self.assertRaisesRegex(ValueError, "max_initial_sources"):
            ProductWorkflowRequest("Acme X100", max_initial_sources=0)
        with self.assertRaisesRegex(ValueError, "minimum_search_priority"):
            ProductWorkflowRequest(
                "Acme X100",
                minimum_search_priority="urgent",  # type: ignore[arg-type]
            )

    def test_provider_provenance_reaches_workflow_metadata(self):
        url = "https://shop.example/product/X100"
        item = candidate(
            url,
            source_type="other",
            authority="unknown",
        )
        fixtures = FixtureServices([item], {url: []})
        attempts = [
            ProviderAttempt(
                "google", "Acme X100", "blocked",
                message="Google bot-check blocked", is_fallback=False,
            ),
            ProviderAttempt(
                "duckduckgo_lite", "Acme X100", "success",
                result_count=1, is_fallback=True,
            ),
        ]
        fixtures.discover_initial = lambda _identity, _market: DiscoveryOutcome(
            [item], "partial", ["Acme X100"], ["Acme X100"], [], attempts,
        )

        result = run_product_workflow(
            ProductWorkflowRequest(
                "Acme X100",
                brand="Acme",
                targeted_search_enabled=False,
            ),
            services=fixtures.services(),
        )

        self.assertEqual(
            [item["provider"] for item in result.summary["provider_attempts"]],
            ["google", "duckduckgo_lite"],
        )
        self.assertTrue(
            result.final_profile.metadata["initial_provider_attempts"][1]["is_fallback"]
        )

    def test_uncorroborated_self_declared_claim_is_not_elevated(self):
        # A brand-domain-consistent page whose own copyright names the
        # brand is a self-declared claim only. With no other trusted page
        # in this run to corroborate it, it must not become "manufacturer"
        # (Stage 18.5 follow-up: self-assertion alone is never sufficient).
        url = "https://acme.example/product/X100"
        item = candidate(url, source_type="other", authority="unknown")
        fixtures = FixtureServices(
            [item],
            {url: [raw("Net weight", "4 kg", url)]},
        )
        html = "<html><body><footer>© 2024 Acme. All rights reserved.</footer></body></html>"

        def fetch(candidate_item):
            fixtures.fetch_calls.append(candidate_item["url"])
            result = fetch_result(candidate_item)
            result["html"] = html
            result["text"] = "Acme X100 © 2024 Acme. All rights reserved."
            return result

        fixtures.fetch = fetch

        result = run_product_workflow(
            ProductWorkflowRequest(
                "Acme X100",
                brand="Acme",
                targeted_search_enabled=False,
            ),
            services=fixtures.services(),
        )

        source = result.fetched_sources[0]
        self.assertEqual(source["authority_status"], "unknown")
        self.assertEqual(source["authority_role"], "unknown")
        net_weight = result.final_profile.by_name.get("net_weight")
        self.assertIsNotNone(net_weight)
        self.assertEqual(net_weight.status, "Unresolved")

    def test_corroborated_manufacturer_claim_is_elevated_and_confirmable(self):
        # A second, already-trusted (SERP-verified) source in the same run
        # links to the candidate's domain - independent, cross-domain
        # corroboration - so the self-declared manufacturer claim may now
        # be elevated, and a fact from it can reach Confirmed.
        anchor_url = "https://acme.example/official"
        dealer_url = "https://acme-shop.example/product/X100"
        anchor_item = candidate(anchor_url, source_type="manufacturer", authority="verified")
        dealer_item = candidate(dealer_url, source_type="other", authority="unknown")
        fixtures = FixtureServices(
            [anchor_item, dealer_item],
            {dealer_url: [raw("Net weight", "4 kg", dealer_url)]},
        )
        anchor_html = '<html><body><a href="https://acme-shop.example/">Official regional site</a></body></html>'
        dealer_html = "<html><body><h1>Acme X100</h1><footer>© 2024 Acme. All rights reserved.</footer></body></html>"

        def fetch(candidate_item):
            fixtures.fetch_calls.append(candidate_item["url"])
            result = fetch_result(candidate_item)
            if candidate_item["url"] == anchor_url:
                result["html"] = anchor_html
                result["text"] = "Find a store"
            else:
                result["html"] = dealer_html
                result["text"] = "Acme X100 © 2024 Acme. All rights reserved."
            return result

        fixtures.fetch = fetch

        result = run_product_workflow(
            ProductWorkflowRequest("Acme X100", brand="Acme", targeted_search_enabled=False),
            services=fixtures.services(),
        )

        dealer_source = next(s for s in result.fetched_sources if s["source_url"] == dealer_url)
        self.assertEqual(dealer_source["authority_status"], "verified")
        self.assertEqual(dealer_source["source_type"], "manufacturer")
        self.assertEqual(dealer_source["authority_role"], "manufacturer")
        net_weight = result.final_profile.by_name.get("net_weight")
        self.assertIsNotNone(net_weight)
        self.assertEqual(net_weight.status, "Confirmed")

    def test_corroborated_dealer_is_verified_but_not_confirmable_alone(self):
        # A corroborated authorized-dealer relationship is real and is
        # marked "verified", but it must not, by itself, unlock Confirmed -
        # exactly like the pre-existing retailer/distributor tier. Authority
        # score/validation semantics are not weakened by this feature.
        anchor_url = "https://acme.example/official"
        dealer_url = "https://acme-shop.example/product/X100"
        anchor_item = candidate(anchor_url, source_type="manufacturer", authority="verified")
        dealer_item = candidate(dealer_url, source_type="other", authority="unknown")
        fixtures = FixtureServices(
            [anchor_item, dealer_item],
            {dealer_url: [raw("Net weight", "4 kg", dealer_url)]},
        )
        anchor_html = '<html><body><a href="https://acme-shop.example/">Authorized dealer</a></body></html>'
        dealer_html = "<html><body><p>Acme Shop is an authorized dealer of Acme.</p></body></html>"

        def fetch(candidate_item):
            fixtures.fetch_calls.append(candidate_item["url"])
            result = fetch_result(candidate_item)
            if candidate_item["url"] == anchor_url:
                result["html"] = anchor_html
                result["text"] = "Find a store"
            else:
                result["html"] = dealer_html
                result["text"] = "Acme Shop is an authorized dealer of Acme."
            return result

        fixtures.fetch = fetch

        result = run_product_workflow(
            ProductWorkflowRequest("Acme X100", brand="Acme", targeted_search_enabled=False),
            services=fixtures.services(),
        )

        dealer_source = next(s for s in result.fetched_sources if s["source_url"] == dealer_url)
        self.assertEqual(dealer_source["authority_status"], "verified")
        self.assertEqual(dealer_source["source_type"], "distributor")
        self.assertEqual(dealer_source["authority_role"], "authorized_dealer")
        net_weight = result.final_profile.by_name.get("net_weight")
        self.assertIsNotNone(net_weight)
        self.assertEqual(net_weight.status, "Unresolved")

    def test_unrelated_domain_content_does_not_gain_authority(self):
        # A retailer whose domain is not brand-consistent must stay
        # "unknown" even if the requested brand appears in its page text.
        url = "https://retailer.example/product/X100"
        item = candidate(url, source_type="other", authority="unknown")
        fixtures = FixtureServices([item], {url: []})
        html = "<html><body><footer>© 2024 Retailer Group</footer></body></html>"

        def fetch(candidate_item):
            fixtures.fetch_calls.append(candidate_item["url"])
            result = fetch_result(candidate_item)
            result["html"] = html
            result["text"] = "Acme X100 sold here © 2024 Retailer Group"
            return result

        fixtures.fetch = fetch

        result = run_product_workflow(
            ProductWorkflowRequest(
                "Acme X100",
                brand="Acme",
                targeted_search_enabled=False,
            ),
            services=fixtures.services(),
        )

        self.assertEqual(result.fetched_sources[0]["authority_status"], "unknown")
        self.assertEqual(result.fetched_sources[0]["source_type"], "other")


class WorkflowPlaywrightLifecycleTests(unittest.TestCase):
    def test_initial_discovery_releases_browser_before_fetch_boundary(self):
        events = []

        class SearchSession:
            active = False
            budget_stage = "discovery"

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                events.append("session_exit")

            def search_with_status(self, query):
                self.active = True
                events.append(f"search:{query}")
                return []

            def release_transient_resources(self):
                events.append("release")
                self.active = False

        search = SearchSession()
        sentinel = object()

        def initial_discovery(_identity, _market, *, searcher):
            searcher("initial")
            return DiscoveryOutcome()

        def targeted_discovery(_identity, query, _market, *, searcher):
            searcher(query)
            return DiscoveryOutcome()

        def workflow_runner(request, services, **_kwargs):
            services.discover_initial(object(), request.market)
            events.append("initial_fetch")
            self.assertFalse(search.active)
            services.discover_targeted(object(), "targeted", request.market)
            events.append("targeted_fetch")
            self.assertFalse(search.active)
            return sentinel

        with patch("core.workflow.ResilientSearchSession", return_value=search), \
                patch("core.workflow.discover_identity_with_status", side_effect=initial_discovery), \
                patch("core.workflow.discover_identity_query_with_status", side_effect=targeted_discovery), \
                patch("core.workflow._run_product_workflow_with_services", side_effect=workflow_runner):
            result = run_product_workflow(ProductWorkflowRequest("Acme X100"))

        self.assertIs(result, sentinel)
        self.assertEqual(events, [
            "search:initial", "release", "initial_fetch",
            "search:targeted", "release", "targeted_fetch", "session_exit",
        ])

    def test_cleanup_runs_for_structured_blocked_discovery(self):
        search = MagicMock()
        blocked = DiscoveryOutcome(search_status="blocked")
        with patch(
            "core.workflow.discover_identity_with_status",
            return_value=blocked,
        ):
            result = _discover_initial_with_released_browser(
                search, object(), "global",
            )

        self.assertIs(result, blocked)
        search.release_transient_resources.assert_called_once_with()

    def test_cleanup_runs_when_discovery_raises(self):
        search = MagicMock()
        with patch(
            "core.workflow.discover_identity_query_with_status",
            side_effect=RuntimeError("discovery error"),
        ):
            with self.assertRaisesRegex(RuntimeError, "discovery error"):
                _discover_targeted_with_released_browser(
                    search, object(), "query", "global",
                )

        search.release_transient_resources.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
