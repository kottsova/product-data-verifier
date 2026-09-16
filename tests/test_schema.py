import unittest

from core.schema import (
    analyze_schema_coverage,
    extend_schema_with_discovered,
    get_attribute_schema,
    get_expected_attributes,
    resolve_attribute_definition,
)
from core.normalize import attribute_label_variants


EXPECTED_BY_CATEGORY = {
    "cooktop": {
        "product_dimensions", "package_dimensions", "net_weight", "gross_weight",
        "connection_rating", "number_of_zones",
    },
    "smartphone": {
        "ram", "storage", "battery_capacity", "charging_power", "display_size",
        "processor", "color",
    },
    "sewing_machine": {
        "machine_type", "shuttle_type", "operation_count", "buttonhole_type",
        "stitch_length", "stitch_width", "power",
    },
    "air_fryer": {
        "power", "capacity", "number_of_bowls", "program_count",
        "temperature_range", "dimensions", "weight",
    },
    "wet_dry_vacuum": {
        "suction_power", "battery_capacity", "runtime", "charging_time",
        "clean_water_tank", "dirty_water_tank", "weight",
    },
}


class SchemaTests(unittest.TestCase):
    def test_expected_attributes_for_regression_categories(self) -> None:
        for category, required in EXPECTED_BY_CATEGORY.items():
            with self.subTest(category=category):
                names = {item.canonical_name for item in get_expected_attributes(category)}
                self.assertTrue(required <= names, required - names)

    def test_logistics_concepts_are_distinct(self) -> None:
        schema = {item.canonical_name: item for item in get_attribute_schema("cooktop")}
        self.assertIn("product_dimensions", schema)
        self.assertIn("package_dimensions", schema)
        self.assertIn("net_weight", schema)
        self.assertIn("gross_weight", schema)
        self.assertNotEqual(schema["product_dimensions"], schema["package_dimensions"])
        self.assertNotEqual(schema["net_weight"], schema["gross_weight"])
        self.assertEqual(schema["package_dimensions"].priority, "high")
        self.assertEqual(schema["gross_weight"].priority, "high")

    def test_ambiguous_dimensions_and_weight_are_not_logistics_guesses(self) -> None:
        self.assertIsNone(resolve_attribute_definition("Dimensions", "cooktop"))
        self.assertIsNone(resolve_attribute_definition("Weight", "cooktop"))
        self.assertEqual(
            resolve_attribute_definition("Package dimensions", "cooktop").canonical_name,
            "package_dimensions",
        )
        self.assertEqual(
            resolve_attribute_definition("Gross weight", "cooktop").canonical_name,
            "gross_weight",
        )

    def test_sewing_accessories_and_presser_feet_are_distinct(self) -> None:
        # "Стандартная комплектация" (standard kit: pedal, manual, bobbins,
        # needles, seam ripper) and "Лапки в комплекте" (included presser
        # feet) are different accessory lists. A single page commonly states
        # both; collapsing them into one canonical field makes validation
        # see two different list values from the same source and flag a
        # conflict where none exists.
        schema = {item.canonical_name: item for item in get_attribute_schema("sewing_machine")}
        self.assertIn("accessories", schema)
        self.assertIn("included_presser_feet", schema)
        self.assertEqual(
            resolve_attribute_definition(
                "Стандартная комплектация", "sewing_machine",
            ).canonical_name,
            "accessories",
        )
        self.assertEqual(
            resolve_attribute_definition(
                "Лапки в комплекте", "sewing_machine",
            ).canonical_name,
            "included_presser_feet",
        )

    def test_smartphone_variant_scope(self) -> None:
        schema = {item.canonical_name: item for item in get_attribute_schema("smartphone")}
        self.assertEqual(schema["processor"].attribute_scope, "model_level")
        self.assertEqual(schema["ram"].attribute_scope, "variant_level")
        self.assertEqual(schema["storage"].attribute_scope, "variant_level")
        self.assertEqual(schema["color"].attribute_scope, "variant_level")

    def test_unknown_discovered_attribute_is_preserved(self) -> None:
        diagnostics = analyze_schema_coverage(
            "smartphone", ["CPU Model", "Peak brightness", "Roller speed"],
        )
        self.assertIn("processor", diagnostics.present)
        self.assertEqual(diagnostics.discovered, ["Peak brightness", "Roller speed"])

        extended = extend_schema_with_discovered(
            "smartphone", ["CPU Model", "Peak brightness", "Roller speed"],
        )
        discovered = {item.canonical_name: item for item in extended if item.scope == "discovered"}
        self.assertIn("peak_brightness", discovered)
        self.assertIn("roller_speed", discovered)
        self.assertFalse(discovered["peak_brightness"].expected)

    def test_coverage_reports_present_and_missing_without_search(self) -> None:
        diagnostics = analyze_schema_coverage(
            "air_fryer", ["Power", "Number of bowls", "Special coating"],
        )
        self.assertIn("power", diagnostics.present)
        self.assertIn("number_of_bowls", diagnostics.present)
        self.assertIn("capacity", diagnostics.missing)
        self.assertEqual(diagnostics.discovered, ["Special coating"])

    def test_regression_language_aliases_are_canonical_and_generic(self) -> None:
        cases = (
            ("cooktop", "Dimensions of the packed product (HxWxD)", "package_dimensions"),
            ("cooktop", "Required niche size for installation (HxWxD)", "installation_dimensions"),
            ("sewing_machine", "Максимальная длина стежка, мм", "stitch_length"),
            ("air_fryer", "Емкость чаш", "capacity"),
            ("air_fryer", "Габариты (Ш×В×Г)", "dimensions"),
            ("wet_dry_vacuum", "შესრუტვის მაქსიმალური სიმძლავრე, კპა", "suction_power"),
            ("wet_dry_vacuum", "ჭუჭყიანი წყლის კონტეინერის მოცულობა, ლ", "dirty_water_tank"),
        )
        for category, alias, expected in cases:
            with self.subTest(category=category, alias=alias):
                definition = resolve_attribute_definition(alias, category)
                self.assertIsNotNone(definition)
                self.assertEqual(definition.canonical_name, expected)

    def test_aliases_do_not_resolve_to_conflicting_canonical_fields(self) -> None:
        for category in EXPECTED_BY_CATEGORY:
            aliases: dict[str, set[str]] = {}
            for definition in get_attribute_schema(category):
                for alias in (definition.canonical_name, *definition.aliases):
                    for key in attribute_label_variants(alias):
                        aliases.setdefault(key, set()).add(definition.canonical_name)
            conflicts = {
                key: names for key, names in aliases.items() if len(names) > 1
            }
            with self.subTest(category=category):
                self.assertEqual(conflicts, {})


if __name__ == "__main__":
    unittest.main()
