import unittest

from core.discovery import DiscoveryIssue, DiscoveryOutcome
from core.extract import RawAttribute
from core.gaps import analyze_gaps
from core.identity import ProductIdentity
from core.mapping import map_attributes
from core.schema import AttributeDefinition
from core.targeted_search import (
    TargetedSearchConfig,
    build_targeted_search_plan,
    run_targeted_search,
)


def raw(name, value):
    return RawAttribute(
        name=name,
        value=value,
        unit=None,
        source_url="https://example.test/product",
        source_type="manufacturer",
        evidence=f"{name}: {value}",
        extraction_method="html_table",
        confidence="high",
        raw_value=value,
        attribute_kind="product",
    )


def identity(**updates):
    values = dict(
        brand="Acme",
        raw_name="Acme X100",
        base_model="X100",
        commercial_model="X100",
        confidence="high",
    )
    values.update(updates)
    return ProductIdentity(**values)


def candidate(url="https://example.test/product", **updates):
    values = {
        "url": url,
        "title": "Acme X100",
        "source_type": "other",
        "authority_status": "unknown",
        "model_match": "exact",
        "model_relevance": "exact_base_model",
        "identity_relation": "same_base_model",
        "identity_verification_evidence": [],
    }
    values.update(updates)
    return values


def fetched(url="https://example.test/product", status="success"):
    return {
        "source_url": url,
        "final_url": url,
        "status": status,
        "http_status": 200,
        "fetch_method": "requests",
        "content_type": "text/html",
        "document_type": "html",
        "html": "<html></html>",
        "text": "",
        "content": b"",
        "pdf_text": "",
        "text_status": "available",
        "blocked_reason": "captcha" if status == "blocked" else None,
        "error": None,
        "source_type": "other",
        "authority_status": "unknown",
        "authority_evidence_url": None,
        "model_relevance": "exact_base_model",
        "identity_relation": "same_base_model",
        "discovery_metadata": {},
    }


def one_field_plan(name, *, scope="unknown", product_identity=None, queries=1):
    product_identity = product_identity or identity()
    definition = AttributeDefinition(name, priority="high", attribute_scope=scope)
    analysis = analyze_gaps(
        "cooktop",
        map_attributes([], category="cooktop"),
        identity=product_identity,
        schema=[definition],
    )
    plan = build_targeted_search_plan(
        analysis,
        product_identity,
        config=TargetedSearchConfig(max_queries_per_field=queries),
    )
    return analysis, plan


class TargetedSearchTests(unittest.TestCase):
    def test_wrong_model_candidate_is_rejected_without_fetch(self):
        analysis, plan = one_field_plan("net_weight")
        calls = []

        def discovery(_identity, _query):
            return DiscoveryOutcome([
                candidate(model_match="mismatch", model_relevance="different_model", identity_relation="different_model")
            ])

        result = run_targeted_search(
            plan, analysis, identity(), discovery=discovery,
            fetcher=lambda item: calls.append(item) or fetched(),
        )
        field = result.fields[0]
        self.assertFalse(field.useful_evidence_found)
        self.assertTrue(field.query_results[0].rejected_candidates)
        self.assertEqual(calls, [])

    def test_incompatible_variant_is_not_silently_accepted(self):
        product = identity(
            product_code="ABC-8-256",
            configuration={"ram": "8 GB", "storage": "256 GB"},
        )
        analysis, plan = one_field_plan("charging_power", scope="variant_level", product_identity=product)

        def discovery(_identity, _query):
            return DiscoveryOutcome([
                candidate(title="Acme X100 12+256GB", identity_relation="same_base_model")
            ])

        result = run_targeted_search(plan, analysis, product, discovery=discovery)
        rejection = result.fields[0].query_results[0].rejected_candidates[0]
        self.assertIn("incompatible ram", rejection.reason)
        self.assertEqual(result.fetch_count, 0)

    def test_blocked_discovery_is_explicit(self):
        analysis, plan = one_field_plan("net_weight", queries=3)
        issue = DiscoveryIssue("blocked", plan.queries[0].query, "Google bot-check blocked")
        result = run_targeted_search(
            plan,
            analysis,
            identity(),
            discovery=lambda _identity, _query: DiscoveryOutcome([], "blocked", [], [], [issue]),
        )
        self.assertTrue(result.blocked)
        self.assertEqual(result.fields[0].search_status, "blocked")
        self.assertEqual(result.fields[0].stop_reason, "discovery_blocked")
        self.assertEqual(result.fields[0].query_results[0].issues, (issue,))

    def test_same_url_is_fetched_once_across_fields_and_queries(self):
        definitions = [
            AttributeDefinition("net_weight", priority="high"),
            AttributeDefinition("gross_weight", priority="high"),
        ]
        analysis = analyze_gaps(
            "cooktop", map_attributes([], category="cooktop"), identity=identity(), schema=definitions,
        )
        plan = build_targeted_search_plan(
            analysis,
            identity(),
            config=TargetedSearchConfig(max_queries_per_field=3),
        )
        fetch_calls = []

        def fetcher(item):
            fetch_calls.append(item["url"])
            return fetched(item["url"])

        result = run_targeted_search(
            plan,
            analysis,
            identity(),
            discovery=lambda _identity, _query: DiscoveryOutcome([candidate()]),
            fetcher=fetcher,
            extractor=lambda _source: [raw("Net weight", "4 kg"), raw("Gross weight", "5 kg")],
        )
        self.assertEqual(result.fetch_count, 1)
        self.assertEqual(fetch_calls, ["https://example.test/product"])
        self.assertTrue(all(field.useful_evidence_found for field in result.fields))

    def test_unrelated_extracted_fact_does_not_resolve_requested_gap(self):
        analysis, plan = one_field_plan("net_weight")
        result = run_targeted_search(
            plan,
            analysis,
            identity(),
            discovery=lambda _identity, _query: DiscoveryOutcome([candidate()]),
            fetcher=lambda _candidate: fetched(),
            extractor=lambda _source: [raw("Voltage", "220 V")],
        )
        query_result = result.fields[0].query_results[0]
        self.assertFalse(query_result.useful_evidence_found)
        self.assertEqual(query_result.extracted_relevant_facts, ())
        self.assertEqual(result.fields[0].search_status, "unresolved")

    def test_related_package_weight_does_not_resolve_net_weight(self):
        analysis, plan = one_field_plan("net_weight")
        result = run_targeted_search(
            plan,
            analysis,
            identity(),
            discovery=lambda _identity, _query: DiscoveryOutcome([candidate()]),
            fetcher=lambda _candidate: fetched(),
            extractor=lambda _source: [raw("Package weight", "5 kg")],
        )
        query_result = result.fields[0].query_results[0]
        self.assertFalse(query_result.useful_evidence_found)
        self.assertEqual(query_result.mapped_candidate_facts, ())
        self.assertEqual(query_result.related_candidate_facts[0].canonical_name, "gross_weight")

    def test_product_dimensions_do_not_resolve_package_dimensions(self):
        analysis, plan = one_field_plan("package_dimensions")
        result = run_targeted_search(
            plan,
            analysis,
            identity(),
            discovery=lambda _identity, _query: DiscoveryOutcome([candidate()]),
            fetcher=lambda _candidate: fetched(),
            extractor=lambda _source: [raw("Product dimensions", "1 x 2 x 3 cm")],
        )
        query_result = result.fields[0].query_results[0]
        self.assertFalse(query_result.useful_evidence_found)
        self.assertEqual(query_result.related_candidate_facts[0].canonical_name, "product_dimensions")

    def test_strong_official_explicit_evidence_stops_query_expansion(self):
        analysis, plan = one_field_plan("net_weight", queries=3)
        discovery_calls = []

        def discovery(_identity, query):
            discovery_calls.append(query)
            return DiscoveryOutcome([candidate(
                source_type="manufacturer", authority_status="verified",
            )])

        result = run_targeted_search(
            plan,
            analysis,
            identity(),
            discovery=discovery,
            fetcher=lambda _candidate: fetched(),
            extractor=lambda _source: [raw("Net weight", "4 kg")],
        )
        self.assertEqual(len(discovery_calls), 1)
        self.assertEqual(result.fields[0].stop_reason, "strong_official_evidence")
        self.assertTrue(result.fields[0].useful_evidence_found)


if __name__ == "__main__":
    unittest.main()
