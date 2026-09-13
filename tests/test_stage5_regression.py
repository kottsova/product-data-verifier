import unittest

from core.extract import RawAttribute
from core.gaps import analyze_gaps
from core.identity import IdentityEvidence, resolve_product_identity
from core.mapping import map_attributes
from core.targeted_search import build_targeted_search_plan


def raw(name, value, *, context=None):
    return RawAttribute(
        name=name,
        value=value,
        unit=None,
        source_url="https://fixture.example/specifications",
        source_type="manufacturer",
        evidence=f"{name}: {value}",
        extraction_method="html_table",
        confidence="high",
        raw_value=value,
        attribute_kind="product",
        context=context,
    )


def resolved(name, model, **extra):
    evidence = [IdentityEvidence("commercial_model", model, "regression identity fixture")]
    evidence.extend(
        IdentityEvidence(field, value, "regression identity fixture")
        for field, value in extra.items()
    )
    return resolve_product_identity(name, evidence=evidence)


class NamedProductStage5RegressionTests(unittest.TestCase):
    def test_bosch_pue611bb5e_gap_characteristics(self):
        identity = resolved("Bosch PUE611BB5E", "PUE611BB5E")
        mapping = map_attributes([
            raw("Dimensions", "51 x 592 x 522 mm"),
            raw("Safety features", "Child lock; residual heat indicator"),
        ], category="cooktop")
        analysis = analyze_gaps("cooktop", mapping, identity=identity)
        self.assertEqual(analysis.by_name["product_dimensions"].gap_state, "ambiguous_existing")
        self.assertEqual(analysis.by_name["package_dimensions"].gap_state, "ambiguous_existing")
        self.assertEqual(analysis.by_name["safety_features"].gap_state, "satisfied")
        self.assertEqual(analysis.by_name["brand"].gap_state, "satisfied")
        self.assertEqual(analysis.by_name["model"].gap_state, "satisfied")
        self.assertNotEqual(
            analysis.by_name["net_weight"].suggested_search_intent,
            analysis.by_name["gross_weight"].suggested_search_intent,
        )

    def test_honor_x8d_gap_characteristics(self):
        identity = resolve_product_identity("HONOR X8d 8+128 Grey 5109CCTW")
        mapping = map_attributes([
            raw("Type", "TFT LCD", context="Display"),
            raw("Size", "6.77 in", context="Display"),
            raw("Battery capacity", "6500 mAh"),
            raw("Product dimensions", "166.9 x 76.8 x 8.4 mm"),
            raw("Weight", "193 g"),
            raw("Color", "16.7 million", context="Display"),
        ], category="smartphone")
        analysis = analyze_gaps("smartphone", mapping, identity=identity)
        for name in ("display_type", "display_size", "battery_capacity", "product_dimensions"):
            self.assertEqual(analysis.by_name[name].gap_state, "satisfied", name)
        self.assertEqual(analysis.by_name["net_weight"].gap_state, "ambiguous_existing")
        self.assertEqual(analysis.by_name["gross_weight"].gap_state, "ambiguous_existing")
        self.assertTrue(analysis.by_name["refresh_rate"].targeted_search_allowed)
        self.assertTrue(analysis.by_name["ip_rating"].targeted_search_allowed)
        self.assertEqual(analysis.by_name["ram"].gap_state, "satisfied")
        self.assertEqual(analysis.by_name["storage"].gap_state, "satisfied")
        self.assertFalse(analysis.by_name["color"].targeted_search_allowed)

    def test_janome_sakura_95_gap_characteristics(self):
        identity = resolved("Janome Sakura 95", "Sakura 95")
        analysis = analyze_gaps(
            "sewing_machine", map_attributes([], category="sewing_machine"), identity=identity,
        )
        for name in (
            "product_dimensions", "package_dimensions", "net_weight", "gross_weight", "reverse",
        ):
            self.assertTrue(analysis.by_name[name].targeted_search_allowed, name)
        self.assertEqual(analysis.by_name["brand"].gap_state, "satisfied")
        self.assertEqual(analysis.by_name["model"].gap_state, "satisfied")

    def test_gressel_gaf_1825_preserves_generic_logistics_ambiguity(self):
        identity = resolved("Gressel GAF-1825", "GAF-1825")
        mapping = map_attributes([
            raw("Weight", "8 kg"),
            raw("Dimensions", "39 x 33 x 36 cm"),
        ], category="air_fryer")
        analysis = analyze_gaps("air_fryer", mapping, identity=identity)
        for name in ("net_weight", "gross_weight", "weight"):
            self.assertEqual(analysis.by_name[name].gap_state, "ambiguous_existing", name)
        for name in ("product_dimensions", "package_dimensions", "dimensions"):
            self.assertEqual(analysis.by_name[name].gap_state, "ambiguous_existing", name)

    def test_dreame_g12_pro_hhr32a_gap_characteristics(self):
        identity = resolved("Dreame G12 Pro HHR32A", "G12 Pro", product_code="HHR32A")
        mapping = map_attributes([
            raw("Self-cleaning", "Yes"),
            raw("Drying temperature", "55 C"),
            raw("Suction power", "16 kPa"),
            raw("Weight", "4.8 kg"),
            raw("Dimensions", "30 x 25 x 110 cm"),
        ], category="wet_dry_vacuum")
        analysis = analyze_gaps("wet_dry_vacuum", mapping, identity=identity)
        for name in ("self_cleaning", "drying_temperature", "suction_power"):
            self.assertEqual(analysis.by_name[name].gap_state, "satisfied", name)
        for name in ("net_weight", "gross_weight", "product_dimensions", "package_dimensions"):
            self.assertEqual(analysis.by_name[name].gap_state, "ambiguous_existing", name)
            self.assertTrue(analysis.by_name[name].targeted_search_allowed, name)
        plan = build_targeted_search_plan(analysis, identity)
        logistics = {
            query.canonical_name for query in plan.queries
            if query.canonical_name in {
                "net_weight", "gross_weight", "product_dimensions", "package_dimensions",
            }
        }
        self.assertEqual(logistics, {
            "net_weight", "gross_weight", "product_dimensions", "package_dimensions",
        })


if __name__ == "__main__":
    unittest.main()
