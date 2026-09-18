"""Stage 31.1: semantic attribute mapping, dedup, noise filtering, and RU/EN
result localization -- regression coverage for the concrete Pixel 9 Pro
smoke-test bug (GSMArena "Chipset"/"CPU" false conflict, a duplicate
"Battery" field next to battery_capacity, and a wall of Korean regulatory
boilerplate from a support.google.com page leaking into the user-facing
profile and CSV export).
"""

from __future__ import annotations

import unittest

from bot.attribute_filter import filter_user_facing, is_user_facing
from bot.export import build_export_rows
from bot.i18n import display_name
from core.extract import RawAttribute
from core.mapping import map_attributes
from core.schema import extend_schema_with_discovered
from services.product_verifier import (
    ServiceAttribute,
    ServiceCategory,
    ServiceEvidence,
    ServiceIdentity,
    VerifyProductRequest,
    VerifyProductResult,
)


def _raw(name: str, value: str, *, unit: str | None = None, source: str = "https://gsmarena.com/x") -> RawAttribute:
    return RawAttribute(
        name=name, value=value, unit=unit, raw_value=value, source_url=source,
        source_type="specialized_reference", evidence=f"{name} | {value}",
        extraction_method="table", confidence="high", attribute_kind="spec", context=None,
    )


class ProcessorChipsetCpuDedupTests(unittest.TestCase):
    """Item 1/2/10: a chipset name and a CPU core-layout string are two
    facets of the same chip, not conflicting values of "processor"."""

    def test_chipset_maps_to_processor(self) -> None:
        items = [_raw("Chipset", "Google Tensor G4 (4 nm)")]
        schema = extend_schema_with_discovered("smartphone", items)
        result = map_attributes(items, schema=schema, category="smartphone")
        self.assertEqual(len(result.mapped), 1)
        self.assertEqual(result.mapped[0].canonical_name, "processor")
        self.assertEqual(result.mapped[0].value, "Google Tensor G4 (4 nm)")

    def test_cpu_core_layout_does_not_conflict_with_chipset(self) -> None:
        items = [
            _raw("Chipset", "Google Tensor G4 (4 nm)"),
            _raw("CPU", "Octa-core (1x3.1 GHz Cortex-X4 & 3x2.6 GHz Cortex-A720 & 4x1.92 GHz Cortex-A520)"),
        ]
        schema = extend_schema_with_discovered("smartphone", items)
        result = map_attributes(items, schema=schema, category="smartphone")
        processor_facts = [item for item in result.mapped if item.canonical_name == "processor"]
        self.assertEqual(len(processor_facts), 1)
        self.assertEqual(processor_facts[0].value, "Google Tensor G4 (4 nm)")
        # The raw CPU-core-layout fact is not discarded -- still visible in
        # the raw layer -- it's just excluded from the processor conflict.
        self.assertIn("CPU", [item.name for item in result.raw_attributes])

    def test_soc_and_platform_are_processor_synonyms(self) -> None:
        for label in ("SoC", "Platform", "System on Chip"):
            with self.subTest(label=label):
                items = [_raw(label, "Example Chip 1")]
                schema = extend_schema_with_discovered("smartphone", items)
                result = map_attributes(items, schema=schema, category="smartphone")
                self.assertEqual(len(result.mapped), 1)
                self.assertEqual(result.mapped[0].canonical_name, "processor")


class BatteryCapacityShapeGuardTests(unittest.TestCase):
    """Item 1/2: bare "Battery" only becomes battery_capacity when its value
    actually looks like a capacity -- never corrupted with an endurance score."""

    def test_battery_with_mah_value_maps_to_capacity(self) -> None:
        items = [_raw("Battery", "4700 mAh")]
        schema = extend_schema_with_discovered("smartphone", items)
        result = map_attributes(items, schema=schema, category="smartphone")
        self.assertEqual(len(result.mapped), 1)
        self.assertEqual(result.mapped[0].canonical_name, "battery_capacity")

    def test_battery_with_endurance_score_does_not_corrupt_capacity(self) -> None:
        items = [_raw("Battery", "50:44h endurance, 1000 cycles")]
        schema = extend_schema_with_discovered("smartphone", items)
        result = map_attributes(items, schema=schema, category="smartphone")
        self.assertFalse(any(item.canonical_name == "battery_capacity" for item in result.mapped))


def _service_attribute(
    canonical_name: str, display_name_: str, value: object, *, discovered: bool,
    status: str = "Confirmed",
) -> ServiceAttribute:
    return ServiceAttribute(
        canonical_name=canonical_name, display_name=display_name_, value=value, unit=None,
        status=status, confidence="high", source="https://support.google.com/x",
        evidence=f"{display_name_} | {value}", priority="low", expected=not discovered,
        discovered=discovered,
    )


class NoiseFilterTests(unittest.TestCase):
    """Item 3: regulatory prose / help instructions / RF-emission tables in a
    language the schema never uses must not reach the user-facing profile."""

    def test_korean_regulatory_row_is_excluded(self) -> None:
        attribute = _service_attribute(
            "umts_대역_i_viii", "Umts 대역 I Viii", "출력 등급 3(< 25.7dBm)", discovered=True,
        )
        self.assertFalse(is_user_facing(attribute))

    def test_korean_help_instructions_on_a_canonical_field_are_excluded(self) -> None:
        # The real Pixel bug: "Wi-Fi" resolved to the canonical wifi field,
        # but the value was Korean how-to-toggle-it prose, not a spec.
        attribute = _service_attribute(
            "wifi", "Wifi",
            "기기에서 설정 앱을 열고 인터넷으로 이동하여 Wi-Fi를 사용 설정 또는 중지합니다.",
            discovered=False,
        )
        self.assertFalse(is_user_facing(attribute))

    def test_manufacturer_address_dump_is_excluded(self) -> None:
        attribute = _service_attribute(
            "guangdong_desay_corporation", "Guangdong Desay Corporation",
            "23rd Floor, Desay Building, 12 Yunshan West Road, Huizhou City, Guangdong Province, China",
            discovered=True,
        )
        self.assertFalse(is_user_facing(attribute))

    def test_discovered_battery_synonym_is_suppressed_as_a_duplicate(self) -> None:
        attribute = _service_attribute(
            "battery", "Battery", "50:44h endurance, 1000 cycles", discovered=True,
        )
        self.assertFalse(is_user_facing(attribute))

    def test_legitimate_canonical_spec_values_are_kept(self) -> None:
        attribute = _service_attribute(
            "display_resolution", "Display Resolution",
            "1280 x 2856 pixels, 20:9 ratio (~495 ppi density)", discovered=False,
        )
        self.assertTrue(is_user_facing(attribute))

    def test_short_useful_discovered_field_is_kept(self) -> None:
        attribute = _service_attribute("water_resistance", "Water Resistance", "IP68", discovered=True)
        self.assertTrue(is_user_facing(attribute))


def _pixel_like_result() -> VerifyProductResult:
    identity = ServiceIdentity(
        brand="Google", base_model="Pixel 9 Pro", commercial_model="Pixel 9 Pro",
        manufacturer_article=None, product_code=None, sku=None, gtin=None,
        color=None, configuration={}, confidence="high",
    )
    category = ServiceCategory(
        category_id="smartphone", category_name="Smartphone",
        parent_category="consumer_electronics", confidence="high",
    )
    attributes = (
        _service_attribute("processor", "Processor", "Google Tensor G4 (4 nm)", discovered=False),
        _service_attribute("battery_capacity", "Battery Capacity", "4700 mAh", discovered=False),
        _service_attribute("battery", "Battery", "50:44h endurance, 1000 cycles", discovered=True),
        _service_attribute("umts_대역_i_viii", "Umts 대역 I Viii", "출력 등급 3(< 25.7dBm)", discovered=True),
    )
    return VerifyProductResult(
        success=True,
        request=VerifyProductRequest(brand="Google", model="Pixel 9 Pro"),
        identity=identity, category=category, attributes=attributes, metadata={},
    )


class ExportLocalizationTests(unittest.TestCase):
    """Item 4/8/9: /export reflects the same filtering as the message, uses
    the chat's chosen display language, and never translates technical
    values/units."""

    def test_noise_and_duplicates_are_absent_from_export_rows(self) -> None:
        rows = build_export_rows(_pixel_like_result(), language="ru")
        canonical_names = {row["canonical_name"] for row in rows}
        self.assertNotIn("battery", canonical_names)
        self.assertNotIn("umts_대역_i_viii", canonical_names)
        self.assertIn("processor", canonical_names)
        self.assertIn("battery_capacity", canonical_names)

    def test_display_name_is_localized_but_value_and_status_are_not(self) -> None:
        ru_rows = {row["canonical_name"]: row for row in build_export_rows(_pixel_like_result(), language="ru")}
        en_rows = {row["canonical_name"]: row for row in build_export_rows(_pixel_like_result(), language="en")}
        self.assertEqual(ru_rows["processor"]["display_name"], "Процессор")
        self.assertEqual(en_rows["processor"]["display_name"], "Processor")
        # Technical value/unit text is identical across languages.
        self.assertEqual(ru_rows["battery_capacity"]["value_text"], en_rows["battery_capacity"]["value_text"])
        self.assertEqual(ru_rows["processor"]["value_text"], "Google Tensor G4 (4 nm)")
        # Status stays a canonical machine value, not a translated word.
        self.assertEqual(ru_rows["processor"]["status"], "Confirmed")
        self.assertEqual(en_rows["processor"]["status"], "Confirmed")


class DisplayNameFallbackTests(unittest.TestCase):
    def test_unlisted_canonical_field_falls_back_without_crashing(self) -> None:
        self.assertTrue(display_name("some_new_field", "ru"))
        self.assertTrue(display_name("some_new_field", "en"))


if __name__ == "__main__":
    unittest.main()
