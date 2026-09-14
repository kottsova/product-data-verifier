import unittest

from core.extract import RawAttribute
from core.identity import IdentityEvidence, ProductIdentity, resolve_product_identity
from core.mapping import DimensionValue, map_attributes
from core.schema import IdentitySchemaFact, analyze_schema_coverage


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
    def test_russian_air_fryer_aliases_map_to_canonical_fields(self) -> None:
        cases = (
            ("Срок гарантии", "warranty"),
            ("Количество чаш", "number_of_bowls"),
            ("Число программ", "program_count"),
            ("Диапазон температур", "temperature_range"),
            ("Тип управления", "control_type"),
            ("Покрытие чаши", "bowl_coating"),
            ("Длина шнура", "cord_length"),
            ("Комплект поставки", "package_contents"),
        )
        for label, expected in cases:
            with self.subTest(label=label):
                result = map_attributes([raw(label, "test value")], category="air_fryer")
                self.assertEqual(result.mapped[0].canonical_name, expected)

    def test_yo_and_ye_are_equivalent_in_attribute_labels(self) -> None:
        result = map_attributes(
            [raw("Объём чаши", "4 л")],
            category="air_fryer",
        )
        self.assertEqual(result.mapped[0].canonical_name, "capacity")

    def test_numbered_bowl_capacity_uses_unambiguous_base_alias(self) -> None:
        result = map_attributes(
            [raw("Объем чаши 1", "4 л"), raw("Объём чаши 2", "4 л")],
            category="air_fryer",
        )
        self.assertEqual(
            [item.canonical_name for item in result.mapped],
            ["capacity", "capacity"],
        )
        self.assertTrue(all(item.mapping_reason == "normalized_alias:capacity" for item in result.mapped))

    def test_punctuation_and_unit_suffixes_do_not_break_mapping(self) -> None:
        cases = (
            ("• Мощность (Вт)", "power"),
            ("Таймер, мин", "timer_range"),
            ("Габариты (Ш×В×Г)", "dimensions"),
            ("Мощность / power (W)", "power"),
        )
        for label, expected in cases:
            with self.subTest(label=label):
                result = map_attributes([raw(label, "100")], category="air_fryer")
                self.assertEqual(result.mapped[0].canonical_name, expected)

    def test_ambiguous_russian_volume_is_not_over_mapped(self) -> None:
        result = map_attributes([raw("Объём, л", "8")], category="air_fryer")
        self.assertEqual(result.mapped, [])
        self.assertEqual(result.ambiguous[0].candidates, ("capacity",))

    def test_existing_english_aliases_remain_unchanged(self) -> None:
        result = map_attributes(
            [raw("Number of programs", "9"), raw("Bowl capacity", "4 L")],
            category="air_fryer",
        )
        self.assertEqual(
            [item.canonical_name for item in result.mapped],
            ["program_count", "capacity"],
        )

    def test_gressel_like_labels_align_without_collapsing_total_capacity_scope(self) -> None:
        total = raw("Общий объем двух чаш", "8 литров")
        result = map_attributes(
            [
                raw("• Мощность", "2700 Вт"),
                raw("Срок гарантии", "12 месяцев"),
                raw("• Объем чаши 1", "4 литра"),
                raw("• Объем чаши 2", "4 литра"),
                total,
            ],
            category="air_fryer",
        )

        self.assertEqual(
            [item.canonical_name for item in result.mapped],
            ["power", "warranty", "capacity", "capacity"],
        )
        self.assertEqual(result.unmapped, [total])

    def test_childlock_maps_to_generic_safety_features(self) -> None:
        item = raw("ChildLock", "Yes")
        mapped = map_attributes([item], category="cooktop").mapped[0]
        self.assertEqual(mapped.canonical_name, "safety_features")
        self.assertEqual(mapped.raw_label, "ChildLock")
        self.assertEqual(mapped.raw_value, "Yes")
        self.assertEqual(mapped.fact_evidence, item.evidence)

    def test_structured_cleaning_and_drying_facts_map_separately(self) -> None:
        cleaning = raw("თვითწმენდის რეჟიმი", "აქვს")
        drying = raw("გაშრობა", "95°C", value="95", unit="°C")
        result = map_attributes([cleaning, drying], category="wet_dry_vacuum")

        self.assertEqual(
            {item.canonical_name for item in result.mapped},
            {"self_cleaning", "drying_temperature"},
        )
        self.assertEqual(
            next(item for item in result.mapped if item.canonical_name == "drying_temperature").raw_value,
            "95°C",
        )

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

    def test_display_type_context(self) -> None:
        result = map_attributes(
            [raw("Type", "AMOLED", context="Display")],
            category="smartphone",
        )
        self.assertEqual(result.mapped[0].canonical_name, "display_type")
        self.assertEqual(result.mapped[0].mapping_reason, "contextual:display_section+type")

    def test_bare_type_is_not_globally_mapped(self) -> None:
        item = raw("Type", "AMOLED")
        result = map_attributes([item], category="smartphone")
        self.assertEqual(result.mapped, [])
        self.assertEqual(result.unmapped, [item])

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
    def test_resolved_identity_satisfies_coverage_with_provenance(self) -> None:
        identity = resolve_product_identity(
            "HONOR X8d",
            evidence=[IdentityEvidence("commercial_model", "X8d", "user input")],
        )
        diagnostics = analyze_schema_coverage(
            "smartphone", map_attributes([], category="smartphone"), identity=identity,
        )

        self.assertIn("brand", diagnostics.present)
        self.assertIn("model", diagnostics.present)
        model_fact = diagnostics.present["model"][0]
        self.assertIsInstance(model_fact, IdentitySchemaFact)
        self.assertEqual(model_fact.identity_field, "commercial_model")
        self.assertIn("Evidence commercial_model=X8d (user input).", model_fact.identity_evidence)
        self.assertNotIn("brand", diagnostics.missing)
        self.assertNotIn("model", diagnostics.missing)

    def test_unresolved_candidate_identifier_does_not_satisfy_model(self) -> None:
        identity = ProductIdentity(
            brand="Gressel",
            raw_name="Gressel GAF-1825",
            confidence="low",
            candidate_identifiers=["GAF-1825"],
            evidence=["Unresolved identifier retained."],
        )
        diagnostics = analyze_schema_coverage(
            "air_fryer", map_attributes([], category="air_fryer"), identity=identity,
        )

        self.assertIn("brand", diagnostics.present)
        self.assertIn("model", diagnostics.missing)
        self.assertNotIn("model", diagnostics.present)
        self.assertFalse(any(fact.value == "GAF-1825" for fact in diagnostics.identity_facts))

    def test_identity_does_not_overwrite_or_duplicate_source_fact(self) -> None:
        source_model = raw("Model", "X8d Pro")
        identity = resolve_product_identity(
            "HONOR X8d",
            evidence=[IdentityEvidence("commercial_model", "X8d", "user input")],
        )
        result = map_attributes([source_model], category="smartphone")
        diagnostics = analyze_schema_coverage("smartphone", result, identity=identity)

        self.assertEqual(
            {str(item.value) for item in diagnostics.present["model"]},
            {"X8d Pro", "X8d"},
        )
        same = resolve_product_identity(
            "HONOR X8d Pro",
            evidence=[IdentityEvidence("commercial_model", "X8d Pro", "user input")],
        )
        same_diagnostics = analyze_schema_coverage("smartphone", result, identity=same)
        self.assertEqual(len(same_diagnostics.present["model"]), 1)

    def test_identity_keeps_distinct_base_and_commercial_model_facts(self) -> None:
        identity = ProductIdentity(
            brand="Acme",
            raw_name="Acme Phone Pro",
            base_model="Phone",
            commercial_model="Phone Pro",
            confidence="high",
            evidence=["Both model fields were explicitly supplied."],
        )
        diagnostics = analyze_schema_coverage(
            "smartphone", map_attributes([], category="smartphone"), identity=identity,
        )

        model_facts = diagnostics.present["model"]
        self.assertEqual({fact.value for fact in model_facts}, {"Phone", "Phone Pro"})
        self.assertEqual(
            {fact.identity_field for fact in model_facts},
            {"base_model", "commercial_model"},
        )

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
