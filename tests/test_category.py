import unittest

from core.category import CategoryResult, detect_category
from core.extract import RawAttribute
from core.identity import resolve_product_identity


def attribute(name: str, value: str = "value") -> RawAttribute:
    return RawAttribute(
        name=name,
        value=value,
        unit=None,
        source_url="https://example.test/product",
        source_type="other",
        evidence=f"{name} | {value}",
        extraction_method="html_table",
        confidence="high",
        raw_value=value,
        attribute_kind="product",
    )


class CategoryDetectionTests(unittest.TestCase):
    def test_bosch_cooktop(self) -> None:
        result = detect_category(
            resolve_product_identity("Bosch PUE611BB5E"),
            product_texts=["Bosch PUE611BB5E induction hob"],
        )
        self.assertEqual(result.category_id, "cooktop")
        self.assertEqual(result.confidence, "high")

    def test_honor_smartphone_from_attribute_signature(self) -> None:
        result = detect_category(
            resolve_product_identity("HONOR X8d"),
            attributes=[
                attribute("CPU Model", "Snapdragon"),
                attribute("GPU", "Adreno"),
                attribute("SIM Card", "Nano SIM"),
            ],
        )
        self.assertEqual(result.category_id, "smartphone")
        self.assertEqual(result.source, "extracted_attributes")

    def test_janome_sewing_machine(self) -> None:
        result = detect_category(
            resolve_product_identity("Janome Sakura 95"),
            product_texts=["Janome Sakura 95 швейная машина"],
        )
        self.assertEqual(result.category_id, "sewing_machine")

    def test_gressel_air_fryer(self) -> None:
        result = detect_category(
            resolve_product_identity("Gressel GAF-1825"),
            product_texts=["Обзор аэрогриля Gressel GAF-1825"],
        )
        self.assertEqual(result.category_id, "air_fryer")

    def test_dreame_wet_dry_vacuum(self) -> None:
        result = detect_category(
            resolve_product_identity("Dreame G12 Pro HHR32A"),
            attributes=[attribute("Suction power", "25 kPa"), attribute("Clean water tank", "1 L")],
            product_texts=["Dreame G12 Pro Wet & Dry"],
        )
        self.assertEqual(result.category_id, "wet_dry_vacuum")
        self.assertEqual(result.source, "mixed")

    def test_structured_product_type_is_strong_evidence(self) -> None:
        result = detect_category(
            resolve_product_identity("Acme X100"),
            attributes=[attribute("Product type", "Smartphone")],
        )
        self.assertEqual(result.category_id, "smartphone")
        self.assertEqual(result.source, "extracted_attributes")

    def test_insufficient_evidence_is_unknown(self) -> None:
        result = detect_category(resolve_product_identity("Acme X100"))
        self.assertEqual(result.category_id, "unknown")
        self.assertEqual(result.source, "unknown")

    def test_weak_generic_tokens_do_not_force_category(self) -> None:
        result = detect_category(
            resolve_product_identity("Acme Series 5 Pro"),
            product_texts=["Smart Pro Series 5"],
        )
        self.assertEqual(result.category_id, "unknown")

    def test_category_result_is_strict(self) -> None:
        with self.assertRaises(ValueError):
            CategoryResult("spaceship", "Spaceship", None, "high", [], "identity")


if __name__ == "__main__":
    unittest.main()
