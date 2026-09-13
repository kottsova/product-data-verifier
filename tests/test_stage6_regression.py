"""Synthetic named-product scenarios for Stage 6 algorithm regression.

These inputs are not fetched source records and must not be reported as real
product findings or fixtures derived from real source data.
"""

import unittest

from core.extract import RawAttribute
from core.gaps import analyze_gaps
from core.identity import IdentityEvidence, resolve_product_identity
from core.mapping import map_attributes
from core.validation import validate_product_profile


def raw(name, value, *, source, source_type="manufacturer", context=None):
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


def resolved(name, model, **extra):
    evidence = [IdentityEvidence("commercial_model", model, "regression identity fixture")]
    evidence.extend(
        IdentityEvidence(field, value, "regression identity fixture")
        for field, value in extra.items()
    )
    return resolve_product_identity(name, evidence=evidence)


def metadata(source_type="manufacturer", authority="verified", relation="exact_variant"):
    return {
        "status": "success",
        "source_type": source_type,
        "authority_status": authority,
        "identity_relation": relation,
    }


class SyntheticNamedProductStage6ScenarioTests(unittest.TestCase):
    def test_bosch_synthetic_profile(self):
        source = "https://bosch.example/PUE611BB5E"
        identity = resolved("Bosch PUE611BB5E", "PUE611BB5E")
        mapping = map_attributes([
            raw("Brand", "Bosch", source=source),
            raw("Model", "PUE611BB5E", source=source),
            raw("ChildLock", "Yes", source=source),
            raw("Dimensions", "51 x 592 x 522 mm", source=source),
        ], category="cooktop")
        gaps = analyze_gaps("cooktop", mapping, identity=identity)
        profile = validate_product_profile(
            identity,
            "cooktop",
            mapping,
            gap_analysis=gaps,
            source_metadata={source: metadata()},
        )
        for name in ("brand", "model", "safety_features"):
            self.assertEqual(profile.by_name[name].status, "Confirmed", name)
            self.assertEqual(profile.by_name[name].authority_status, "verified")
        for name in ("product_dimensions", "package_dimensions"):
            self.assertEqual(profile.by_name[name].status, "Unresolved", name)
            self.assertEqual(profile.by_name[name].resolution_reason, "ambiguous_scope")
        for name in ("net_weight", "gross_weight"):
            self.assertEqual(profile.by_name[name].status, "Unresolved", name)

    def test_honor_synthetic_profile(self):
        source = "https://honor.example/X8d"
        identity = resolve_product_identity("HONOR X8d 8+128 Grey 5109CCTW")
        mapping = map_attributes([
            raw("Brand", "HONOR", source=source),
            raw("Model", "X8d", source=source),
            raw("Type", "TFT LCD", source=source, context="Display"),
            raw("Size", "6.77 in", source=source, context="Display"),
            raw("Battery capacity", "6500 mAh", source=source),
            raw("Product dimensions", "166.9 x 76.8 x 8.4 mm", source=source),
            raw("RAM", "8 GB", source=source),
            raw("Storage", "128 GB", source=source),
            raw("Weight", "193 g", source=source),
            raw("Color", "16.7 million", source=source, context="Display"),
        ], category="smartphone")
        gaps = analyze_gaps("smartphone", mapping, identity=identity)
        profile = validate_product_profile(
            identity,
            "smartphone",
            mapping,
            gap_analysis=gaps,
            source_metadata={source: metadata()},
        )
        for name in (
            "display_type", "display_size", "battery_capacity", "product_dimensions",
            "ram", "storage",
        ):
            self.assertEqual(profile.by_name[name].status, "Confirmed", name)
        self.assertEqual(profile.by_name["net_weight"].resolution_reason, "ambiguous_scope")
        self.assertEqual(profile.by_name["color"].status, "Unresolved")
        self.assertEqual(profile.by_name["color"].resolution_reason, "ambiguous_scope")

    def test_janome_synthetic_profile(self):
        source = "https://janome.example/Sakura95"
        identity = resolved("Janome Sakura 95", "Sakura 95")
        mapping = map_attributes([
            raw("Brand", "Janome", source=source),
            raw("Model", "Sakura 95", source=source),
            raw("Machine type", "Electromechanical", source=source),
            raw("Shuttle type", "Vertical oscillating", source=source),
            raw("Operation count", "15", source=source),
            raw("Reverse", "Yes", source=source),
        ], category="sewing_machine")
        profile = validate_product_profile(
            identity,
            "sewing_machine",
            mapping,
            source_metadata={source: metadata()},
        )
        for name in ("machine_type", "shuttle_type", "operation_count", "reverse"):
            self.assertEqual(profile.by_name[name].status, "Confirmed", name)
        for name in ("product_dimensions", "package_dimensions", "net_weight", "gross_weight"):
            self.assertEqual(profile.by_name[name].status, "Unresolved", name)
            self.assertEqual(profile.by_name[name].resolution_reason, "no_valid_evidence")

    def test_gressel_synthetic_profile_keeps_specialized_source_secondary(self):
        source = "https://ixbt.example/gressel-gaf-1825"
        identity = resolved("Gressel GAF-1825", "GAF-1825")
        mapping = map_attributes([
            raw("Power", "2400 W", source=source, source_type="specialized_reference"),
            raw("Bowl capacity", "9 l", source=source, source_type="specialized_reference"),
            raw("Program count", "12", source=source, source_type="specialized_reference"),
            raw("Weight", "8 kg", source=source, source_type="specialized_reference"),
            raw("Dimensions", "39 x 33 x 36 cm", source=source, source_type="specialized_reference"),
        ], category="air_fryer")
        gaps = analyze_gaps("air_fryer", mapping, identity=identity)
        profile = validate_product_profile(
            identity,
            "air_fryer",
            mapping,
            gap_analysis=gaps,
            source_metadata={source: metadata("specialized_reference", "unknown")},
        )
        for name in ("power", "capacity", "program_count"):
            fact = profile.by_name[name]
            self.assertEqual(fact.status, "Confirmed", name)
            self.assertEqual(fact.confidence, "medium")
            self.assertEqual(fact.resolution_reason, "specialized_exact_model_explicit")
            self.assertNotEqual(fact.authority_status, "verified")
        self.assertEqual(profile.by_name["net_weight"].resolution_reason, "ambiguous_scope")
        self.assertEqual(profile.by_name["product_dimensions"].resolution_reason, "ambiguous_scope")

    def test_dreame_synthetic_profile_does_not_upgrade_retailer(self):
        synthetic_official = "https://dreame.example/G12-Pro-HHR32A"
        synthetic_retailer = "https://shop.example/G12-Pro-HHR32A"
        identity = resolved("Dreame G12 Pro HHR32A", "G12 Pro", product_code="HHR32A")
        mapping = map_attributes([
            raw("Brand", "Dreame", source=synthetic_official),
            raw("Model", "G12 Pro", source=synthetic_official),
            raw("Suction power", "16 kPa", source=synthetic_official),
            raw("Self-cleaning", "Yes", source=synthetic_official),
            raw("Drying temperature", "55 C", source=synthetic_official),
            raw("Rated power", "300 W", source=synthetic_retailer, source_type="retailer"),
            raw("Weight", "4.8 kg", source=synthetic_retailer, source_type="retailer"),
        ], category="wet_dry_vacuum")
        gaps = analyze_gaps("wet_dry_vacuum", mapping, identity=identity)
        profile = validate_product_profile(
            identity,
            "wet_dry_vacuum",
            mapping,
            gap_analysis=gaps,
            source_metadata={
                synthetic_official: metadata(),
                synthetic_retailer: metadata("retailer", "unknown"),
            },
        )
        for name in ("suction_power", "self_cleaning", "drying_temperature"):
            self.assertEqual(profile.by_name[name].status, "Confirmed", name)
        rated = profile.by_name["rated_power"]
        self.assertEqual(rated.status, "Unresolved")
        self.assertEqual(rated.authority_status, "unknown")
        self.assertEqual(rated.resolution_reason, "insufficient_source_quality")
        self.assertEqual(profile.by_name["net_weight"].resolution_reason, "ambiguous_scope")


if __name__ == "__main__":
    unittest.main()
