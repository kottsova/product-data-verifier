import unittest

from core.identity import (
    IdentityEvidence,
    ProductIdentity,
    AttributeScopeDecision,
    base_model_in_text,
    compare_identities,
    compare_identity_names,
    identity_verification_signals,
    resolve_product_identity,
)
from core.discovery import clear_official_domain_cache, discover_identity, discover_identity_with_status


class IdentityParsingTests(unittest.TestCase):
    def test_honor_messy_input(self) -> None:
        identity = resolve_product_identity("HONOR X8d 8+128 Grey 5109CCTW")
        self.assertEqual(identity.brand, "HONOR")
        self.assertEqual(identity.base_model, "X8d")
        self.assertEqual(identity.configuration, {"ram": "8 GB", "storage": "128 GB"})
        self.assertEqual(identity.color, "Grey")
        self.assertEqual(identity.candidate_identifiers, ["5109CCTW"])
        self.assertNotIn("5109CCTW", identity.base_model)
        self.assertIsNone(identity.sku)
        self.assertIsNone(identity.manufacturer_article)

    def test_configuration_formats(self) -> None:
        for value in ("8+128", "8/128", "8GB+128GB", "12+256", "12GB 256GB"):
            with self.subTest(value=value):
                identity = resolve_product_identity(f"HONOR X8d {value}")
                expected_ram = "12 GB" if value.startswith("12") else "8 GB"
                expected_storage = "256 GB" if "256" in value else "128 GB"
                self.assertEqual(identity.configuration["ram"], expected_ram)
                self.assertEqual(identity.configuration["storage"], expected_storage)

    def test_arbitrary_number_pair_is_not_configuration(self) -> None:
        identity = resolve_product_identity("Acme Cutter 7+19")
        self.assertEqual(identity.configuration, {})
        self.assertEqual(identity.base_model, "Cutter 7+19")

    def test_safe_model_phrase_matching(self) -> None:
        self.assertTrue(base_model_in_text("Sakura 95", "Janome Sakura95 купить"))
        self.assertTrue(base_model_in_text("G12 Pro", "Dreame G12 Pro Wet & Dry"))
        self.assertFalse(base_model_in_text("iPhone 18", "Apple iPhone 18 Pro"))

    def test_evidenced_product_code_is_separate_from_commercial_model(self) -> None:
        identity = resolve_product_identity(
            "Dreame G12 Pro HHR32A",
            evidence=[
                IdentityEvidence("commercial_model", "G12 Pro", "manufacturer product page"),
                IdentityEvidence("product_code", "HHR32A", "manufacturer metadata"),
            ],
        )
        self.assertEqual(identity.commercial_model, "G12 Pro")
        self.assertEqual(identity.base_model, "G12 Pro")
        self.assertEqual(identity.product_code, "HHR32A")
        self.assertIsNone(identity.manufacturer_article)

    def test_suffix_has_no_implicit_market_meaning(self) -> None:
        identity = resolve_product_identity("Bosch PUE611BB5E/01")
        self.assertEqual(identity.base_model, "PUE611BB5E")
        self.assertEqual(identity.variant_suffix, "01")
        self.assertEqual(identity.market_scope, "unknown")
        self.assertIsNone(identity.market_hint)

    def test_gtin_is_optional(self) -> None:
        identity = resolve_product_identity("Bosch PUE611BB5E")
        self.assertIsNone(identity.gtin)
        self.assertEqual(identity.confidence, "high")

    def test_identity_structure_rejects_invalid_scope(self) -> None:
        with self.assertRaises(ValueError):
            ProductIdentity(
                brand="Acme", raw_name="Acme X1", base_model="X1",
                market_scope="local",  # type: ignore[arg-type]
            )

    def test_attribute_scope_is_unknown_without_evidence(self) -> None:
        self.assertEqual(AttributeScopeDecision().attribute_scope, "unknown")
        with self.assertRaises(ValueError):
            AttributeScopeDecision(attribute_scope="all")  # type: ignore[arg-type]

    def test_evidence_rejects_unsupported_field(self) -> None:
        with self.assertRaises(ValueError):
            IdentityEvidence("guessed_code", "X1")  # type: ignore[arg-type]


class IdentityComparisonTests(unittest.TestCase):
    def test_honor_configuration_is_same_base_model(self) -> None:
        result = compare_identity_names("HONOR X8d", "HONOR X8d 8+128 Grey")
        self.assertEqual(result.relation, "same_base_model")

    def test_iphone_pro_is_different_model(self) -> None:
        result = compare_identity_names("Apple iPhone 18", "Apple iPhone 18 Pro")
        self.assertEqual(result.relation, "different_model")

    def test_janome_spacing_is_equivalent(self) -> None:
        result = compare_identity_names("Janome Sakura 95", "Janome Sakura95")
        self.assertEqual(result.relation, "same_base_model")

    def test_bosch_suffix_is_same_base_model(self) -> None:
        result = compare_identity_names("Bosch PUE611BB5E", "Bosch PUE611BB5E/01")
        self.assertEqual(result.relation, "same_base_model")

    def test_bosch_adjacent_model_is_different(self) -> None:
        result = compare_identity_names("Bosch PUE611BB5E", "Bosch PUE611BB5F")
        self.assertEqual(result.relation, "different_model")

    def test_samsung_suffix_is_same_base_model_not_region(self) -> None:
        right = resolve_product_identity("Samsung WW90T554CAT/LP")
        result = compare_identities(resolve_product_identity("Samsung WW90T554CAT"), right)
        self.assertEqual(result.relation, "same_base_model")
        self.assertEqual(right.variant_suffix, "LP")
        self.assertEqual(right.market_scope, "unknown")

    def test_matching_gtin_can_confirm_exact_variant(self) -> None:
        evidence = [IdentityEvidence("gtin", "1234567890123", "package barcode")]
        left = resolve_product_identity("Acme X100", evidence=evidence)
        right = resolve_product_identity("Acme X100", evidence=evidence)
        self.assertEqual(compare_identities(left, right).relation, "exact_variant")

    def test_missing_gtin_does_not_downgrade_model_relation(self) -> None:
        left = resolve_product_identity("Acme X100")
        right = resolve_product_identity(
            "Acme X100", evidence=[IdentityEvidence("gtin", "1234567890123")]
        )
        self.assertEqual(compare_identities(left, right).relation, "same_base_model")

    def test_discovery_signals_keep_unresolved_code_untyped(self) -> None:
        identity = resolve_product_identity("HONOR X8d 8+128 Grey 5109CCTW")
        signals = identity_verification_signals(identity)
        self.assertEqual(signals["unresolved_identifier_1"], "5109CCTW")
        self.assertNotIn("sku", signals)


class IdentityDiscoveryTests(unittest.TestCase):
    def test_discovery_uses_base_model_not_literal_messy_input(self) -> None:
        calls = []

        def searcher(query):
            calls.append(query)
            if "official" in query:
                return [("https://honor.example/", "HONOR official website")]
            return [(
                "https://honor.example/phones/honor-x8d/spec",
                "HONOR X8d Specifications",
            )]

        identity = resolve_product_identity("HONOR X8d 8+128 Grey 5109CCTW")
        candidates = discover_identity(identity, searcher=searcher)

        self.assertTrue(any("HONOR X8d" in query for query in calls))
        self.assertFalse(any(identity.raw_name in query for query in calls))
        official = next(item for item in candidates if "/spec" in item["url"])
        self.assertEqual(official["identity_relation"], "same_base_model")
        self.assertEqual(official["model_match"], "exact")
        self.assertEqual(official["model_relevance"], "exact_base_model")

        outcome = discover_identity_with_status(identity, searcher=searcher)
        self.assertIn("HONOR X8d", outcome.queries)
        self.assertFalse(any(identity.raw_name in query for query in outcome.queries))

    def test_variant_signals_are_optional_secondary_evidence(self) -> None:
        def searcher(query):
            return [(
                "https://shop.example/honor-x8d-5109CCTW",
                "HONOR X8d 8+128 Grey 5109CCTW",
            )]

        identity = resolve_product_identity("HONOR X8d 8+128 Grey 5109CCTW")
        candidate = discover_identity(identity, searcher=searcher)[0]
        evidence = candidate["identity_verification_evidence"]
        self.assertIn("color=Grey", evidence)
        self.assertIn("unresolved_identifier_1=5109CCTW", evidence)
        self.assertIn("configuration=8 GB+128 GB", evidence)

    def test_multi_token_model_can_be_base_model_source(self) -> None:
        identity = resolve_product_identity("Janome Sakura 95")

        def searcher(query):
            return [(
                "https://janome.example/shveynaya-mashina-janome-sakura-95-kupit",
                "Janome Sakura95 купить швейную машину",
            )]

        candidate = discover_identity(identity, searcher=searcher)[0]
        self.assertEqual(candidate["identity_relation"], "same_base_model")
        self.assertEqual(candidate["model_match"], "exact")

    def test_identity_aware_rescoring_re_reranks_candidates(self) -> None:
        clear_official_domain_cache()
        identity = resolve_product_identity(
            "Janome Sakura 95",
            brand="Janome",
            evidence=[IdentityEvidence("commercial_model", "Sakura 95", "input model")],
        )
        homepage = ("https://janome.example/", "Janome Official Website")
        product = (
            "https://janome.example/shveynaya-mashina-janome-sakura-95-kupit",
            "Швейная машина Janome Sakura 95",
        )

        def searcher(query):
            if query == "Janome official website":
                return [homepage]
            return [product]

        outcome = discover_identity_with_status(identity, searcher=searcher)

        self.assertIn("sakura-95", outcome.candidates[0]["url"])
        self.assertEqual(outcome.candidates[0]["model_relevance"], "exact_base_model")
        self.assertEqual(len(outcome.candidates), 1)
        self.assertEqual(outcome.rejected_candidates[0]["url"], homepage[0])
        self.assertEqual(
            outcome.rejected_candidates[0]["relevance_relation"], "reject",
        )

    def test_model_extension_does_not_become_exact_base_phrase(self) -> None:
        identity = resolve_product_identity("Apple iPhone 18")

        def searcher(query):
            return [(
                "https://apple.example/iphone-18-pro",
                "Apple iPhone 18 Pro",
            )]

        outcome = discover_identity_with_status(identity, searcher=searcher)
        self.assertEqual(outcome.candidates, [])
        self.assertEqual(len(outcome.rejected_candidates), 1)
        candidate = outcome.rejected_candidates[0]
        self.assertEqual(candidate["model_match"], "different_variant")
        self.assertEqual(candidate["relevance_relation"], "reject")

    def test_bosch_suffix_is_variant_relevance_but_same_base_identity(self) -> None:
        identity = resolve_product_identity("Bosch PUE611BB5E")

        def searcher(query):
            return [(
                "https://bosch.example/product/PUE611BB5E-01",
                "Bosch PUE611BB5E/01",
            )]

        candidate = discover_identity(identity, searcher=searcher)[0]
        self.assertEqual(candidate["model_match"], "likely_variant")
        self.assertEqual(candidate["model_relevance"], "variant_of_base_model")
        self.assertEqual(candidate["identity_relation"], "same_base_model")
        self.assertEqual(candidate["market_scope"], "unknown")

    def test_adjacent_bosch_model_is_different(self) -> None:
        identity = resolve_product_identity("Bosch PUE611BB5E")

        def searcher(query):
            return [(
                "https://shop.example/product/PUE611BB5F",
                "Bosch PUE611BB5F",
            )]

        outcome = discover_identity_with_status(identity, searcher=searcher)
        self.assertEqual(outcome.candidates, [])
        candidate = outcome.rejected_candidates[0]
        self.assertEqual(candidate["model_relevance"], "different_model")
        self.assertEqual(candidate["relevance_relation"], "reject")


if __name__ == "__main__":
    unittest.main()
