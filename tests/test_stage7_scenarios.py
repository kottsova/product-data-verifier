"""Synthetic Stage 7 profile/export scenarios, not factual product claims."""

import unittest

from core.export import profile_to_dict
from tests.test_profile_export import candidate, definition, final_profile


class SyntheticRepresentativeProfileExportScenarios(unittest.TestCase):
    def test_bosch_like_cooktop_profile(self):
        profile = final_profile([
            definition("safety_features"),
            definition("product_dimensions"),
            definition("package_dimensions"),
        ], [candidate("safety_features", "Child lock")])
        data = profile_to_dict(profile)
        self.assertEqual(data["attributes"][0]["status"], "Confirmed")
        self.assertEqual(data["unresolved"], [
            "product_dimensions", "package_dimensions",
        ])

    def test_honor_like_smartphone_profile(self):
        profile = final_profile([
            definition("display_type"),
            definition("battery_capacity"),
            definition("net_weight"),
        ], [
            candidate("display_type", "TFT LCD"),
            candidate("battery_capacity", "6500", unit="mAh"),
        ], category="smartphone")
        data = profile_to_dict(profile)
        self.assertEqual(
            [item["canonical_name"] for item in data["attributes"]],
            ["display_type", "battery_capacity", "net_weight"],
        )
        self.assertEqual(data["unresolved"], ["net_weight"])

    def test_janome_like_sewing_machine_profile(self):
        profile = final_profile([
            definition("operation_count"),
            definition("reverse"),
        ], [candidate("operation_count", "15")], category="sewing_machine")
        data = profile_to_dict(profile)
        self.assertEqual(data["attributes"][0]["status"], "Confirmed")
        self.assertEqual(data["attributes"][1]["status"], "Unresolved")
        self.assertEqual(data["attributes"][1]["value"], None)

    def test_gressel_like_specialized_reference_profile(self):
        profile = final_profile([definition("power")], [candidate(
            "power",
            "2400",
            unit="W",
            source="https://reference.example/product",
            source_type="specialized_reference",
            authority="unknown",
        )], category="air_fryer")
        item = profile_to_dict(profile)["attributes"][0]
        self.assertEqual(item["status"], "Confirmed")
        self.assertEqual(item["confidence"], "medium")
        self.assertEqual(item["authority_status"], "unknown")
        self.assertEqual(item["supporting_sources"][0]["source_type"], "specialized_reference")

    def test_dreame_like_retailer_is_not_upgraded(self):
        profile = final_profile([
            definition("self_cleaning"),
            definition("rated_power"),
        ], [
            candidate("self_cleaning", "Yes"),
            candidate(
                "rated_power",
                "300",
                unit="W",
                source="https://retailer.example/product",
                source_type="retailer",
                authority="unknown",
            ),
        ], category="wet_dry_vacuum")
        data = profile_to_dict(profile)
        by_name = {item["canonical_name"]: item for item in data["attributes"]}
        self.assertEqual(by_name["self_cleaning"]["status"], "Confirmed")
        self.assertEqual(by_name["rated_power"]["status"], "Unresolved")
        self.assertEqual(by_name["rated_power"]["authority_status"], "unknown")


if __name__ == "__main__":
    unittest.main()
