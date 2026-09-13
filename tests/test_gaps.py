import unittest

from core.extract import RawAttribute
from core.gaps import analyze_gaps
from core.identity import IdentityEvidence, resolve_product_identity
from core.mapping import MappingResult, map_attributes
from core.schema import AttributeDefinition
from core.targeted_search import TargetedSearchConfig, build_targeted_search_plan


def raw(name, value, *, context=None):
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
        context=context,
    )


def identity(name="Acme X100"):
    return resolve_product_identity(
        name,
        evidence=[IdentityEvidence("commercial_model", name.split(" ", 1)[1], "test")],
    )


class GapAnalysisTests(unittest.TestCase):
    def test_satisfied_canonical_field_is_not_searched(self):
        mapping = map_attributes([raw("Net weight", "4.2 kg")], category="cooktop")
        result = analyze_gaps("cooktop", mapping, identity=identity())
        gap = result.by_name["net_weight"]
        self.assertEqual(gap.gap_state, "satisfied")
        self.assertFalse(gap.targeted_search_allowed)

    def test_missing_high_priority_field_is_searchable(self):
        result = analyze_gaps("cooktop", map_attributes([], category="cooktop"), identity=identity())
        gap = result.by_name["net_weight"]
        self.assertEqual(gap.gap_state, "missing_searchable")
        self.assertTrue(gap.targeted_search_allowed)

    def test_missing_low_priority_field_is_deferred(self):
        definition = AttributeDefinition("minor_detail", priority="low")
        result = analyze_gaps(
            "cooktop", map_attributes([], category="cooktop"), identity=identity(), schema=[definition],
        )
        gap = result.by_name["minor_detail"]
        self.assertEqual(gap.gap_state, "missing_low_priority")
        self.assertFalse(gap.targeted_search_allowed)

    def test_configurable_priority_threshold_defers_medium(self):
        definition = AttributeDefinition("medium_detail", priority="medium")
        result = analyze_gaps(
            "cooktop",
            map_attributes([], category="cooktop"),
            identity=identity(),
            schema=[definition],
            minimum_search_priority="high",
        )
        self.assertEqual(result.by_name["medium_detail"].gap_state, "missing_low_priority")

    def test_ambiguous_weight_creates_separate_net_and_gross_gaps(self):
        mapping = map_attributes([raw("Weight", "4.7 kg")], category="cooktop")
        result = analyze_gaps("cooktop", mapping, identity=identity())
        self.assertEqual(result.by_name["net_weight"].gap_state, "ambiguous_existing")
        self.assertEqual(result.by_name["gross_weight"].gap_state, "ambiguous_existing")
        self.assertTrue(result.by_name["net_weight"].targeted_search_allowed)
        plan = build_targeted_search_plan(result, identity())
        queries = [item.query.casefold() for item in plan.queries if "weight" in item.canonical_name]
        self.assertTrue(any("net weight" in query for query in queries))
        self.assertTrue(any("gross weight" in query for query in queries))
        self.assertFalse(any(query.endswith(" x100 weight") for query in queries))

    def test_ambiguous_dimensions_keep_product_and_package_separate(self):
        mapping = map_attributes([raw("Dimensions", "10 x 20 x 30 cm")], category="cooktop")
        result = analyze_gaps("cooktop", mapping, identity=identity())
        self.assertEqual(result.by_name["product_dimensions"].gap_state, "ambiguous_existing")
        self.assertEqual(result.by_name["package_dimensions"].gap_state, "ambiguous_existing")
        self.assertEqual(
            result.by_name["product_dimensions"].suggested_search_intent,
            "product dimensions",
        )
        self.assertEqual(
            result.by_name["package_dimensions"].suggested_search_intent,
            "package dimensions",
        )

    def test_identity_brand_and_model_are_satisfied_without_search(self):
        result = analyze_gaps("smartphone", map_attributes([], category="smartphone"), identity=identity())
        for name in ("brand", "model"):
            with self.subTest(name=name):
                self.assertEqual(result.by_name[name].gap_state, "satisfied")
                self.assertFalse(result.by_name[name].targeted_search_allowed)

    def test_unrelated_discovered_attribute_does_not_satisfy_field(self):
        mapping = map_attributes([raw("Peak brightness boost", "1800 nit")], category="smartphone")
        result = analyze_gaps("smartphone", mapping, identity=identity())
        self.assertEqual(result.by_name["battery_capacity"].gap_state, "missing_searchable")
        self.assertNotIn(raw("Peak brightness boost", "1800 nit"), result.by_name["battery_capacity"].known_related_facts)

    def test_package_weight_does_not_satisfy_net_weight(self):
        mapping = map_attributes([raw("Package weight", "5 kg")], category="cooktop")
        result = analyze_gaps("cooktop", mapping, identity=identity())
        self.assertEqual(result.by_name["gross_weight"].gap_state, "satisfied")
        self.assertEqual(result.by_name["net_weight"].gap_state, "missing_searchable")
        self.assertTrue(result.by_name["net_weight"].known_related_facts)

    def test_product_dimensions_do_not_satisfy_package_dimensions(self):
        mapping = map_attributes([raw("Product dimensions", "1 x 2 x 3 cm")], category="cooktop")
        result = analyze_gaps("cooktop", mapping, identity=identity())
        self.assertEqual(result.by_name["product_dimensions"].gap_state, "satisfied")
        self.assertEqual(result.by_name["package_dimensions"].gap_state, "missing_searchable")

    def test_display_color_ambiguity_is_not_searched_as_variant_color(self):
        mapping = map_attributes([raw("Color", "1.07 billion", context="Display")], category="smartphone")
        result = analyze_gaps("smartphone", mapping, identity=identity())
        gap = result.by_name["color"]
        self.assertEqual(gap.gap_state, "ambiguous_existing")
        self.assertFalse(gap.targeted_search_allowed)
        self.assertIn("Display color", gap.reason)

    def test_query_includes_stable_identity_signal(self):
        result = analyze_gaps("cooktop", map_attributes([], category="cooktop"), identity=identity())
        plan = build_targeted_search_plan(
            result, identity(), config=TargetedSearchConfig(max_queries_per_field=2),
        )
        self.assertTrue(plan.queries)
        self.assertTrue(all("Acme" in item.query and "X100" in item.query for item in plan.queries))
        self.assertTrue(all(len(items) <= 2 for items in plan.by_field.values()))

    def test_deterministic_mapping_bug_is_not_searched_around(self):
        item = raw("Net weight", "4.2 kg")
        broken_mapping = MappingResult(raw_attributes=[item], unmapped=[item])
        result = analyze_gaps("cooktop", broken_mapping, identity=identity())
        gap = result.by_name["net_weight"]
        self.assertEqual(gap.gap_state, "missing_searchable")
        self.assertFalse(gap.targeted_search_allowed)
        self.assertIn("mapping issue", gap.reason)


if __name__ == "__main__":
    unittest.main()
