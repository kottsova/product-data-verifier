"""Stage 32.1: localization is a pure projection of one canonical result."""

from __future__ import annotations

import csv
from copy import deepcopy
from io import StringIO
import unittest

from bot.attribute_filter import filter_user_facing
from bot.export import export_result_csv
from bot.formatters import format_result, found_counts
from bot.i18n import display_name
from bot.localize import localize_value
from tests.test_stage32_full_spec import LanguageParityTests


class DynamicLabelFallbackTests(unittest.TestCase):
    def test_unknown_future_attribute_gets_both_labels(self):
        self.assertEqual(display_name("haptic_engine", "en", fallback="Haptic Engine"), "Haptic Engine")
        self.assertEqual(display_name("haptic_engine", "ru", fallback="Haptic Engine"), "Вибромотор")

    def test_semantic_alias_precedes_generic_token_fallback(self):
        self.assertEqual(display_name("camera_sensor_shift", "en"), "Camera Sensor Shift")
        self.assertEqual(display_name("camera_sensor_shift", "ru"), "Сдвиг сенсора камеры")

    def test_dynamic_alias_is_localized_deterministically(self):
        first = display_name("haptic_levels", "ru")
        self.assertEqual(first, "Уровни виброотклика")
        self.assertEqual(display_name("haptic_levels", "ru"), first)

    def test_untranslatable_technical_label_is_preserved(self):
        self.assertEqual(display_name("ultra_flux_codec", "ru", fallback="Ultra Flux Codec"), "Ultra Flux Codec")


class SemanticValueLocalizationTests(unittest.TestCase):
    def test_camera_features_are_localized_per_item(self):
        value = "Night Sight; Macro Focus; Portrait Mode; Super Res Zoom up to 30x"
        self.assertEqual(
            localize_value("camera_features", value, None, "ru"),
            "Ночной режим; Макрофокус; Портретный режим; Super Res Zoom до 30×",
        )

    def test_generic_colors_change_but_commercial_colors_do_not(self):
        self.assertEqual(localize_value("color", "Black; White; Green; Blue", None, "ru"),
                         "Чёрный; Белый; Зелёный; Синий")
        commercial = "Obsidian; Porcelain; Hazel; Rose Quartz"
        self.assertEqual(localize_value("color", commercial, None, "ru"), commercial)

    def test_box_contents_are_localized_per_item_and_interfaces_survive(self):
        self.assertEqual(
            localize_value("package_contents", "1 m USB-C to USB-C cable (USB 2.0); SIM tool; Documentation", None, "ru"),
            "Кабель USB-C—USB-C длиной 1 м (USB 2.0); Инструмент для SIM-карты; Документация",
        )

    def test_sensors_authentication_charging_and_materials(self):
        self.assertEqual(
            localize_value("sensors", "Accelerometer; Gyroscope; Proximity sensor; Ambient light sensor; Barometer", None, "ru"),
            "Акселерометр; Гироскоп; Датчик приближения; Датчик освещённости; Барометр",
        )
        self.assertEqual(
            localize_value("authentication", "Fingerprint Unlock; Face Unlock; Pattern, PIN, Password", None, "ru"),
            "Разблокировка по отпечатку пальца; Разблокировка по лицу; Графический ключ, PIN-код, Пароль",
        )
        self.assertEqual(
            localize_value("charging_types", "Wired charging; Wireless charging; Fast charging; Reverse wireless charging", None, "ru"),
            "Проводная зарядка; Беспроводная зарядка; Быстрая зарядка; Обратная беспроводная зарядка",
        )
        self.assertEqual(
            localize_value("materials_and_durability", "Aluminum; Glass; Stainless steel", None, "ru"),
            "Алюминий; Стекло; Нержавеющая сталь",
        )
        self.assertEqual(localize_value("frame_material", "Aluminum", None, "ru"), "Алюминий")
        self.assertEqual(localize_value("fast_charging", "Fast charging", None, "ru"), "Быстрая зарядка")

    def test_technical_and_proprietary_names_are_preserved(self):
        for canonical, value in (
            ("processor", "Google Tensor G4"), ("gpu", "Mali-G715 MC7"),
            ("usb", "USB Type-C 3.2"), ("wifi", "Wi-Fi 7"),
            ("bluetooth", "Bluetooth 5.3"), ("ip_rating", "IP68"),
            ("network_generations", "5G Sub-6; mmWave"),
            ("video_formats", "HEVC (H.265), AVC (H.264)"),
        ):
            self.assertEqual(localize_value(canonical, value, None, "ru"), value, canonical)


class OneCanonicalResultParityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Reuse the already-built deterministic Stage 32 result. No second
        # verification/provider run is performed for the alternate language.
        LanguageParityTests.setUpClass()
        cls.result = LanguageParityTests.service

    def test_en_and_ru_are_pure_presentations_of_one_result(self):
        before = deepcopy(self.result)
        en = format_result(self.result, language="en")
        ru = format_result(self.result, language="ru")
        self.assertEqual(self.result, before)
        self.assertEqual(self.result.conflicts, before.conflicts)
        self.assertEqual(self.result.unresolved, before.unresolved)
        self.assertEqual(
            [(a.canonical_name, a.value, a.source, a.supporting_sources) for a in self.result.attributes],
            [(a.canonical_name, a.value, a.source, a.supporting_sources) for a in before.attributes],
        )
        self.assertEqual(found_counts(self.result), found_counts(before))
        self.assertEqual(sum(line.count(" = ") for line in en), sum(line.count(" = ") for line in ru))

    def test_csv_count_order_and_population_are_identical(self):
        en = list(csv.reader(StringIO(export_result_csv(self.result, language="en"))))
        ru = list(csv.reader(StringIO(export_result_csv(self.result, language="ru"))))
        self.assertEqual(len(en[0]), len(ru[0]))
        self.assertEqual(len(en[1]), len(ru[1]))
        self.assertEqual(
            [bool(value and value != "Not found") for value in en[1]],
            [bool(value and value != "Не найдено") for value in ru[1]],
        )
        attributes = filter_user_facing(self.result.attributes)
        # Four identity columns precede attributes and two source columns
        # follow them; each localized position still maps to the same canonical.
        self.assertEqual(
            en[0][4:-2],
            [display_name(a.canonical_name, "en", fallback=a.display_name) for a in attributes],
        )
        self.assertEqual(
            ru[0][4:-2],
            [display_name(a.canonical_name, "ru", fallback=a.display_name) for a in attributes],
        )

    def test_stage32_dynamic_haptic_attribute_is_localized(self):
        canonical = {item.canonical_name: item for item in self.result.attributes}
        self.assertIn("haptic_engine", canonical)
        self.assertEqual(canonical["haptic_engine"].value, "Linear resonant")
        self.assertIn("Haptic Engine = Linear resonant", "\n".join(format_result(self.result, language="en")))
        self.assertIn("Вибромотор = Linear resonant", "\n".join(format_result(self.result, language="ru")))


if __name__ == "__main__":
    unittest.main()
