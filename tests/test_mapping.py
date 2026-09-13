import unittest

from core.extract import RawAttribute
from core.mapping import DimensionValue, map_attributes
from core.schema import analyze_schema_coverage


def raw(
    name: str,
    raw_value: str,
    *,
    value: str | None = None,
    unit: str | None = None,
    context: str | None = None,
    source_url: str = "https://example.test/product",
) -> RawAttribute:
    return RawAttribute(
        name=name,
        value=value if value is not None else raw_value,
        unit=unit,
        source_url=source_url,
        source_type="manufacturer",
        evidence=f"source fact: {name} = {raw_value}",
        extraction_method="html_table",
        confidence="high",
        raw_value=raw_value,
        attribute_kind="product",
        context=context,
    )


class DirectMappingTests(unittest.TestCase):
    def test_strong_weight_aliases(self) -> None:
        cases = (
            ("Net weight", "net_weight"),
            ("Вес нетто", "net_weight"),
            ("Gross weight", "gross_weight"),
            ("Package weight", "gross_weight"),
            ("Shipping weight", "gross_weight"),
        )
        for label, expected in cases:
            with self.subTest(label=label):
                result = map_attributes([raw(label, "4.2 kg")], category="cooktop")
                self.assertEqual(result.mapped[0].canonical_name, expected)
                self.assertEqual(result.mapped[0].mapping_confidence, "high")
                self.assertEqual(result.mapped[0].mapping_reason, f"explicit_scope:{expected}")

    def test_strong_dimension_aliases(self) -> None:
        cases = (
            ("Package dimensions", "package_dimensions"),
            ("Packaging dimensions", "package_dimensions"),
            ("Product dimensions", "product_dimensions"),
            ("Item dimensions", "product_dimensions"),
        )
        for label, expected in cases:
            with self.subTest(label=label):
                result = map_attributes([raw(label, "10 × 20 × 30 cm")], category="cooktop")
                self.assertEqual(result.mapped[0].canonical_name, expected)
                self.assertEqual(result.mapped[0].mapping_confidence, "high")

    def test_direct_mapping_preserves_provenance(self) -> None:
        item = raw("Net weight", "4.2 kg", value="4.2", unit="KG")
        mapped = map_attributes([item], category="cooktop").mapped[0]
        self.assertEqual(mapped.raw_label, "Net weight")
        self.assertEqual(mapped.raw_value, "4.2 kg")
        self.assertEqual(mapped.source_url, item.source_url)
        self.assertEqual(mapped.source_type, item.source_type)
        self.assertEqual(mapped.fact_evidence, item.evidence)
        self.assertEqual(mapped.mapping_reason, "explicit_scope:net_weight")
        self.assertEqual(mapped.mapping_confidence, "high")
        self.assertEqual(mapped.unit, "kg")
        self.assertEqual(mapped.contributors, (item,))


class AmbiguityTests(unittest.TestCase):
    def test_generic_labels_do_not_create_canonical_facts(self) -> None:
        cases = (
            ("Weight", "net_weight"),
            ("Dimensions", "product_dimensions"),
            ("Capacity", "battery_capacity"),
            ("Size", "display_size"),
        )
        for label, forbidden in cases:
            with self.subTest(label=label):
                result = map_attributes([raw(label, "100")], category="smartphone")
                self.assertFalse(any(item.canonical_name == forbidden for item in result.canonical_attributes))
                self.assertEqual(result.ambiguous[0].raw_attribute.name, label)

    def test_unknown_attribute_survives_mapping(self) -> None:
        item = raw("Peak brightness boost", "1800 nit")
        result = map_attributes([item], category="smartphone")
        self.assertEqual(result.raw_attributes, [item])
        self.assertEqual(result.unmapped, [item])
        self.assertEqual(result.canonical_attributes, [])

    def test_bare_color_is_not_assumed_to_be_product_variant_color(self) -> None:
        result = map_attributes(
            [raw("Color", "1.07 billion colors", context="Display")],
            category="smartphone",
        )
        self.assertFalse(any(item.canonical_name == "color" for item in result.canonical_attributes))
        self.assertEqual(result.ambiguous[0].candidates, ("color",))


class ContextualMappingTests(unittest.TestCase):
    def test_battery_capacity_context(self) -> None:
        result = map_attributes(
            [raw("Capacity", "5000 mAh", context="Battery")],
            category="smartphone",
        )
        mapped = result.mapped[0]
        self.assertEqual(mapped.canonical_name, "battery_capacity")
        self.assertEqual(mapped.mapping_confidence, "medium")
        self.assertEqual(mapped.mapping_reason, "contextual:battery_section+capacity")

    def test_display_size_context(self) -> None:
        result = map_attributes(
            [raw("Size", "6.7 in", context="Display / Screen")],
            category="smartphone",
        )
        self.assertEqual(result.mapped[0].canonical_name, "display_size")
        self.assertEqual(result.mapped[0].mapping_reason, "contextual:display_section+size")

    def test_packaging_weight_context(self) -> None:
        result = map_attributes(
            [raw("Weight", "5 kg", context="Packaging")],
            category="cooktop",
        )
        self.assertEqual(result.mapped[0].canonical_name, "gross_weight")
        self.assertNotEqual(result.mapped[0].canonical_name, "net_weight")


class CompositeDimensionTests(unittest.TestCase):
    def test_product_dimensions_are_composed_with_provenance(self) -> None:
        items = [
            raw("Height", "10 cm", value="10", unit="CM", context="Product dimensions"),
            raw("Width", "20 cm", value="20", unit="cm", context="Product dimensions"),
            raw("Depth", "30 cm", value="30", unit="centimeters", context="Product dimensions"),
        ]
        result = map_attributes(items, category="cooktop")
        self.assertEqual(len(result.derived), 1)
        derived = result.derived[0]
        self.assertEqual(derived.canonical_name, "product_dimensions")
        self.assertEqual(derived.mapping_confidence, "medium")
        self.assertEqual(derived.mapping_reason, "composed:height+width+depth:product_dimensions")
        self.assertEqual(derived.contributors, tuple(items))
        self.assertEqual(derived.value, DimensionValue("10", "20", "30", "cm"))
        self.assertTrue(derived.derived)

    def test_package_dimensions_are_composed_separately(self) -> None:
        items = [
            raw("Height", "10 mm", value="10", unit="mm", context="Package measurements"),
            raw("Width", "20 mm", value="20", unit="mm", context="Package measurements"),
            raw("Depth", "30 mm", value="30", unit="mm", context="Package measurements"),
        ]
        result = map_attributes(items, category="cooktop")
        self.assertEqual(result.derived[0].canonical_name, "package_dimensions")
        self.assertNotEqual(result.derived[0].canonical_name, "product_dimensions")

    def test_product_and_package_components_are_not_mixed(self) -> None:
        items = [
            raw("Height", "10 mm", value="10", unit="mm", context="Product dimensions"),
            raw("Width", "20 mm", value="20", unit="mm", context="Product dimensions"),
            raw("Depth", "30 mm", value="30", unit="mm", context="Package dimensions"),
        ]
        result = map_attributes(items, category="cooktop")
        self.assertEqual(result.derived, [])

    def test_different_sources_are_not_composed(self) -> None:
        items = [
            raw("Height", "10 mm", value="10", unit="mm", context="Product dimensions", source_url="https://one.test"),
            raw("Width", "20 mm", value="20", unit="mm", context="Product dimensions", source_url="https://one.test"),
            raw("Depth", "30 mm", value="30", unit="mm", context="Product dimensions", source_url="https://two.test"),
        ]
        result = map_attributes(items, category="cooktop")
        self.assertEqual(result.derived, [])

    def test_two_components_or_mixed_units_are_not_composed(self) -> None:
        two = [
            raw("Height", "10 mm", value="10", unit="mm", context="Product dimensions"),
            raw("Width", "20 mm", value="20", unit="mm", context="Product dimensions"),
        ]
        self.assertEqual(map_attributes(two, category="cooktop").derived, [])
        mixed = [
            *two,
            raw("Depth", "3 cm", value="3", unit="cm", context="Product dimensions"),
        ]
        self.assertEqual(map_attributes(mixed, category="cooktop").derived, [])


class SchemaIntegrationTests(unittest.TestCase):
    def test_coverage_uses_canonical_mapping_and_keeps_unknown(self) -> None:
        net = raw("Net weight", "4.2 kg")
        unknown = raw("Special coating", "Ceramic X")
        result = map_attributes([net, unknown], category="cooktop")
        diagnostics = analyze_schema_coverage("cooktop", result)
        self.assertIn("net_weight", diagnostics.present)
        self.assertNotIn("net_weight", diagnostics.missing)
        self.assertEqual(diagnostics.discovered, [unknown])

    def test_mapped_raw_facts_remain_available(self) -> None:
        item = raw("Product dimensions", "10 × 20 × 30 cm")
        result = map_attributes([item], category="cooktop")
        self.assertEqual(result.raw_attributes, [item])
        self.assertEqual(result.mapped[0].contributors, (item,))

    def test_ambiguous_mapping_does_not_satisfy_schema(self) -> None:
        item = raw("Color", "1.07 billion colors", context="Display")
        result = map_attributes([item], category="smartphone")
        diagnostics = analyze_schema_coverage("smartphone", result)
        self.assertNotIn("color", diagnostics.present)
        self.assertIn("color", diagnostics.missing)
        self.assertEqual(diagnostics.discovered, [item])


if __name__ == "__main__":
    unittest.main()
