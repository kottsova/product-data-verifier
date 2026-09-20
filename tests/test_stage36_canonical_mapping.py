"""Stage 36: category-aware canonical mapping of normalised attributes (offline, generic)."""

from __future__ import annotations

from dataclasses import replace
from fractions import Fraction
import json
from pathlib import Path
import re
import unittest

from core.canonical_mapping import (
    AMBIGUOUS, CONCEPTS, MAPPED, NOT_A_SPEC, UNMAPPED, detect_collisions, format_number, map_attribute, map_result,
    to_canonical, unit_token,
)
from core.category import CATEGORY_NAMES, CategoryResult, detect_category
from core.normalization import SPEC, NormalizationResult, normalize_attributes
from core.raw_extraction import RawAttribute
from diagnostics.stage34_extraction import PRODUCTS, slug
from diagnostics.stage35_normalization import network_forbidden
from diagnostics.stage36_canonical_mapping import result_from_payload
from services.canonical_mapping import CanonicalMappingService

ROOT = Path(__file__).resolve().parents[1]
URL = "https://brand.example/en/p/abc1234x"


def category(category_id: str) -> CategoryResult:
    name, parent = CATEGORY_NAMES[category_id]
    return CategoryResult(category_id, name, parent, "high", [], "identity")


def raw(label, value, section="", method="table_row", url=URL):
    return RawAttribute(label, value, url, "official_product_page", "visible_dom", method, section)


def normalised(*records, model="ABC1234X"):
    attributes, _, _ = normalize_attributes(list(records), model)
    return [a for a in attributes if a.cls == SPEC]


def rows(label, value, cat="unknown", section="", **kw):
    """The full pipeline for one attribute (mapping, then the not-a-spec check for what stayed unmapped)."""
    attributes = tuple(normalised(raw(label, value, section, **kw)))
    result = map_result(NormalizationResult("P", "B", "M", attributes, (), 1, {}, {}), category(cat))
    return list(result.fields)


def one(label, value, cat="unknown", section="", **kw):
    (row,) = rows(label, value, cat, section, **kw)
    return row


class Units(unittest.TestCase):
    def test_exact_conversions(self):
        self.assertEqual(to_canonical("length", "in", Fraction(1)), Fraction(254, 10))
        self.assertEqual(to_canonical("length", "cm", Fraction(20.29).limit_denominator(100)), Fraction(2029, 10))
        self.assertEqual(format_number(to_canonical("mass", "oz", Fraction(1))), ("0.02835", True))

    def test_formatting_flags_rounding_only_when_needed(self):
        self.assertEqual(format_number(Fraction(1, 4)), ("0.25", False))
        self.assertEqual(format_number(Fraction(1, 3)), ("0.333333", True))
        self.assertEqual(format_number(Fraction(190)), ("190", False))

    def test_fahrenheit_is_affine(self):
        self.assertEqual(to_canonical("temperature", "°F", Fraction(212)), Fraction(100))
        self.assertEqual(to_canonical("temperature", "°F", Fraction(32)), Fraction(0))

    def test_units_outside_the_family_do_not_convert(self):
        self.assertIsNone(to_canonical("mass", "mm", Fraction(1)))
        self.assertEqual(unit_token("Вт"), "W")
        self.assertEqual(unit_token("Tage"), "d")

    def test_conversion_result_keeps_the_original(self):
        f = one("Assembled Product Height", "20.29 cm", "power_tool")
        self.assertEqual((f.canonical_key, f.normalized_value, f.unit, f.unit_status), ("height_mm", "202.9", "mm", "converted"))
        self.assertEqual(f.conversion["original_number"], "20.29")
        self.assertEqual(f.conversion["original_unit"], "cm")

    def test_dual_measure_alternate_is_preserved_and_preferred(self):
        f = one("Width", "3.32 in (84.3 mm)")
        self.assertEqual((f.canonical_key, f.normalized_value), ("width_mm", "84.3"))
        self.assertEqual(f.conversion["method"], "source_alternate")
        self.assertEqual(f.conversion["alternates"], ["84.3 mm"])
        self.assertIn("3.32", f.conversion["original_value"])

    def test_alternate_tolerates_coarse_rounding_of_the_primary(self):
        f = one("Weight", "0.06 oz (1.68 g)")
        self.assertEqual((f.canonical_key, f.normalized_value, f.conversion["method"]), ("weight_kg", "0.00168", "source_alternate"))

    def test_arithmetic_conversion_without_alternate(self):
        f = one("Weight", "10 oz")
        self.assertEqual((f.normalized_value, f.conversion["method"]), ("0.283495", "unit_conversion"))
        self.assertTrue(f.conversion["rounded"])

    def test_bpm_and_ipm_are_the_same_rate(self):
        f = one("No Load Speed", "34000 bpm", "power_tool")
        self.assertEqual((f.canonical_key, f.normalized_value, f.unit), ("impact_rate_ipm", "34000", "ipm"))

    def test_unit_family_selects_the_key_for_the_same_label(self):
        self.assertEqual(one("No Load Speed", "2000 rpm", "power_tool").canonical_key, "no_load_speed_rpm")
        self.assertEqual(one("No Load Speed", "34000 bpm", "power_tool").canonical_key, "impact_rate_ipm")


class ImpliedUnit(unittest.TestCase):
    def test_single_plausible_unit_is_inferred_but_never_strong(self):
        f = one("Weight net", "37.3")
        self.assertEqual((f.canonical_key, f.normalized_value, f.unit, f.unit_status), ("weight_kg", "37.3", "kg", "implied"))
        self.assertLessEqual(f.confidence, 0.75)
        self.assertIn("unit not written", " ".join(f.reasons))

    def test_dimensions_without_unit(self):
        f = one("Product dimensions", "149 × 330 × 305")
        self.assertEqual((f.canonical_key, f.unit_status, f.unit), ("dimensions_mm", "implied", "mm"))

    def test_ambiguous_unit_is_not_guessed(self):
        f = one("Weight", "141")             # 141 kg and 141 g are both plausible
        self.assertEqual(f.status, AMBIGUOUS)
        self.assertEqual(f.canonical_key, "")
        self.assertIn("plausible as kg|g", f.candidates[0].reasons[0])
        f = one("Warranty", "3")               # months or years
        self.assertEqual(f.status, AMBIGUOUS)
        f = one("Dimensions", "42.5х40.5х33")  # Cyrillic х; mm and cm are both plausible
        self.assertEqual(f.status, AMBIGUOUS)

    def test_temperature_without_unit(self):
        f = one("Temperature range", "80–200", "air_fryer")
        self.assertEqual((f.canonical_key, f.normalized_value, f.unit, f.unit_status), ("temperature_range_c", "80–200", "°C", "implied"))


class Multilingual(unittest.TestCase):
    CASES = {
        "weight_kg": [("Nettogewicht", "37.3 kg"), ("Poids net", "37.3 kg"), ("Peso netto", "37.3 kg"), ("Hmotnost", "37.3 kg"),
                      ("Weight net", "37.3 kg"), ("Вес", "37.3 кг")],
        "power_w": [("Anschlusswert", "3600 W"), ("Puissance de raccordement", "3600 W"), ("Potenza", "3600 W"), ("Příkon", "3600 W"),
                    ("Rated power", "3600 W"), ("Мощность", "3600 Вт")],
        "voltage_v": [("Spannung", "230 V"), ("Tension", "230 V"), ("Tensione", "230 V"), ("Napětí", "230 V"), ("Voltage", "230 V"),
                      ("Напряжение", "230 В")],
        "frequency_hz": [("Frequenz", "50 Hz"), ("Fréquence", "50 Hz"), ("Frequenza", "50 Hz"), ("Jmenovitá frekvence", "50 Hz"),
                         ("Frequency", "50 Hz"), ("Частота", "50 Гц")],
        "usable_volume_l": [("Nutzinhalt (des Innenraums)", "71 L"), ("Volume utile (de la cavité)", "71 L"),
                            ("Volume utile (dei vani)", "71 L"), ("Use vol cavity", "71 L")],
        "cleaning_system": [("Reinigungssystem", "Pyrolyse"), ("Système de nettoyage", "Pyrolyse"), ("Sistema di pulizia", "Pyrolyse"),
                            ("Cleaning system", "Pyrolyse")],
    }

    def test_labels_of_one_concept_collapse_into_one_key(self):
        for key, cases in self.CASES.items():
            for label, value in cases:
                f = one(label, value, "oven")
                self.assertEqual((f.status, f.canonical_key), (MAPPED, key), (label, value))

    def test_measures_written_in_other_languages_agree(self):
        values = {one(label, value, "oven").normalized_value for label, value in self.CASES["weight_kg"] + self.CASES["power_w"][:3]}
        self.assertEqual(values, {"37.3", "3600"})

    def test_word_units_in_values(self):
        for text, expected in (("12 месяцев", "12"), ("2 Jahre eingeschränkte Gewährleistung", "24"), ("2 year limited warranty", "24"),
                               ("3 года", None)):
            label = "Срок гарантии" if expected is None or "месяц" in text else "Warranty"
            f = one(label, text)
            if expected is not None:
                self.assertEqual((f.canonical_key, f.normalized_value, f.unit), ("warranty_months", expected, "month"), text)
        self.assertEqual(one("Срок службы", "3 года").canonical_key, "service_life_years")
        self.assertEqual(one("Operating time", "14 days", "oral_care").normalized_value, "336")
        self.assertEqual(one("Betriebsdauer", "14 Tage", "oral_care").normalized_value, "336")

    def test_localised_number_forms(self):
        self.assertEqual(one("Speed", "62,000 brush movements/min", "oral_care").normalized_value, "62000")
        self.assertEqual(one("Geschwindigkeit", "62.000 Bürstenkopfbewegungen/Min", "oral_care").normalized_value, "62000")
        self.assertEqual(one("Stromversorgung", "100 bis 240 V", "oral_care").normalized_value, "100–240")

    def test_boolean_words_in_several_languages(self):
        for label, value in (("Schnellaufheizung", "ja (verfügbar mit App)"), ("Préchauffage rapide", "Oui"), ("Preriscaldamento rapido", "Sì"),
                             ("Fast preheat", "yes")):
            f = one(label, value, "oven")
            self.assertEqual((f.canonical_key, f.normalized_value), ("fast_preheat", "true"), label)


class Context(unittest.TestCase):
    def test_group_decides_the_key(self):
        f = one("- Steel", "13mm", "power_tool", "Specifications > Drilling capacity")
        self.assertEqual((f.canonical_key, f.normalized_value, f.unit), ("drilling_capacity_steel_mm", "13", "mm"))
        self.assertIn("group_context", f.mapping_method)
        self.assertEqual(one("- Steel", "13mm", "power_tool", "Specifications").status, UNMAPPED)  # no group: never a bare `steel_mm`

    def test_same_label_in_different_groups(self):
        hi_speed = one("- Hi", "0-2,000rpm", "power_tool", "Specifications > No load speed")
        hi_rate = one("- Hi", "0-30,000ipm", "power_tool", "Specifications > Impacts per minute")
        self.assertEqual((hi_speed.canonical_key, hi_rate.canonical_key), ("no_load_speed_high_rpm", "impact_rate_high_ipm"))
        self.assertEqual(one("- Lo", "0-500rpm", "power_tool", "Specifications > No load speed").canonical_key, "no_load_speed_low_rpm")

    def test_group_is_case_and_language_insensitive(self):
        f = one("- Holz", "38mm", "power_tool", "Technische Daten > Bohrleistung")
        self.assertEqual(f.canonical_key, "drilling_capacity_wood_mm")

    def test_category_selects_the_key(self):
        self.assertEqual(one("Voltage", "18 V", "power_tool").canonical_key, "battery_voltage_v")
        self.assertEqual(one("Voltage", "230 V", "power_tool").canonical_key, "voltage_v")
        self.assertEqual(one("Voltage", "230 V", "oven").canonical_key, "voltage_v")
        self.assertEqual(one("Voltage", "18 V", "oven").canonical_key, "voltage_v")
        self.assertEqual(one("Timer", "yes", "air_fryer").canonical_key, "timer_available")
        self.assertEqual(one("Timer", "BrushPacer", "oral_care").canonical_key, "brushing_timer")

    def test_category_specific_concepts_do_not_leak(self):
        self.assertEqual(one("Chuck Type", "Keyless", "power_tool").canonical_key, "chuck_type")
        self.assertEqual(one("Chuck Type", "Keyless", "oven").status, UNMAPPED)
        self.assertEqual(one("Chuck Type", "Keyless", "unknown").status, UNMAPPED)
        self.assertEqual(one("Weight", "1.2 kg", "unknown").canonical_key, "weight_kg")   # universal concepts survive unknown category

    def test_generic_label_resolved_by_category(self):
        self.assertEqual(one("Capacity", "71 L", "oven").canonical_key, "usable_volume_l")
        self.assertEqual(one("Capacity", "4 L", "air_fryer").canonical_key, "bowl_capacity_l")
        self.assertEqual(one("Capacity", "4 L", "unknown").status, UNMAPPED)

    def test_generic_group_is_not_a_signal_for_long_titles(self):
        title = "Very long product page title with many words describing body colour housing and more things"
        f = one("Material", "steel", "air_fryer", title)
        self.assertNotEqual((f.status, f.canonical_key), (MAPPED, "body_material"))
        g = one("Material", "Metal", "coffee_machine", "Pump colourmate")
        self.assertEqual((g.status, g.canonical_key), (MAPPED, "body_material"))

    def test_repeated_measurement_block_does_not_overwrite_the_product(self):
        records = [raw(label, value, "specs > spec", "json_state_pair") for label, value in (
            ("Height", "4.92 in (124.9 mm)"), ("Width", "3.32 in (84.3 mm)"), ("Height", "0.24 in (6.11 mm)"), ("Width", "0.57 in (14.4 mm)"))]
        result = map_result(NormalizationResult("P", "B", "M", tuple(normalised(*records)), (), 4, {}, {}), category("computer_peripheral"))
        keys = [(f.canonical_key, f.normalized_value) for f in result.fields]
        self.assertEqual(keys, [("height_mm", "124.9"), ("width_mm", "84.3"), ("secondary_component_height_mm", "6.11"),
                                ("secondary_component_width_mm", "14.4")])
        self.assertEqual(result.fields[2].mapping_method, "repeated_block_context")
        self.assertEqual(result.fields[2].context["primary_key"], "height_mm")

    def test_axis_order_in_label_splits_dimensions(self):
        fields = rows("Abmessungen des Gerätes (H x B x T)", "595 x 594 x 548 mm", "oven")
        got = {f.canonical_key: (f.normalized_value, f.mapping_method) for f in fields}
        self.assertEqual(got["dimensions_mm"][0], "595 × 594 × 548")
        self.assertEqual([got[k][0] for k in ("height_mm", "width_mm", "depth_mm")], ["595", "594", "548"])
        self.assertEqual(got["height_mm"][1], "dimension_split")
        plain = rows("Rozměry výrobku", "149 x 330 x 305 mm", "coffee_machine")
        self.assertEqual([f.canonical_key for f in plain], ["dimensions_mm"])            # order unknown: no guessing
        self.assertIn("unknown", str(plain[0].context))

    def test_compound_value_yields_companion_field(self):
        fields = rows("Напряжение", "220-240 В, 50/60 Гц", "air_fryer")
        self.assertEqual([(f.canonical_key, f.normalized_value, f.unit) for f in fields],
                         [("voltage_v", "220–240", "V"), ("frequency_hz", "50–60", "Hz")])
        self.assertEqual(fields[1].mapping_method, "compound_value_split")

    def test_measure_embedded_in_text(self):
        f = one("Wireless range", "32.8 ft (10 m)*. (*Wireless range may vary)", "computer_peripheral")
        self.assertEqual((f.canonical_key, f.normalized_value, f.unit), ("wireless_range_m", "10", "m"))
        g = one("DPI (Minimal and maximal value)", "200-8000 DPI (can be set in increments of 50 DPI)", "computer_peripheral")
        self.assertEqual((g.canonical_key, g.normalized_value), ("dpi_range", "200–8000"))
        b = rows("Battery type", "Rechargeable Li-Po (500 mAh) battery", "computer_peripheral")
        self.assertEqual([(f.canonical_key, f.normalized_value, f.unit) for f in b],
                         [("battery_type", "Rechargeable Li-Po (500 mAh) battery", ""), ("battery_capacity_ah", "0.5", "Ah")])


class Ambiguity(unittest.TestCase):
    def test_weak_label_is_never_mapped(self):
        f = one("Connection", "3600 W", "oven")
        self.assertEqual(f.status, AMBIGUOUS)
        self.assertEqual([c.canonical_key for c in f.candidates], ["power_w"])
        self.assertLess(f.candidates[0].confidence, 0.70)
        self.assertEqual((f.canonical_key, f.normalized_value), ("", ""))

    def test_value_that_does_not_fit_keeps_candidates_and_reasons(self):
        f = one("Overall dimensions", "182 mm", "power_tool")
        self.assertEqual(f.status, AMBIGUOUS)
        self.assertEqual(f.candidates[0].canonical_key, "dimensions_mm")
        self.assertIn("single value", f.candidates[0].reasons[0])
        g = one("Battery life", "Get three hours of use from a one-minute quick charge", "computer_peripheral")
        self.assertEqual((g.status, g.candidates[0].canonical_key), (AMBIGUOUS, "battery_runtime_h"))

    def test_voltage_between_the_two_regimes_is_ambiguous(self):
        f = one("Voltage", "75 V", "power_tool")
        self.assertEqual(f.status, AMBIGUOUS)
        self.assertEqual({c.canonical_key for c in f.candidates}, {"battery_voltage_v", "voltage_v"})

    def test_generic_labels_stay_unmapped(self):
        self.assertEqual(one("Type", "Cordless", "power_tool").status, UNMAPPED)
        self.assertEqual(one("Something entirely different", "1 pc", "power_tool").status, UNMAPPED)

    def test_wrong_unit_family_is_rejected_not_forced(self):
        f = one("Power supply", "100–240 V", "oral_care")
        self.assertEqual(f.canonical_key, "voltage_v")           # not power_w
        f = one("Power", "18 V", "oral_care")
        self.assertNotEqual((f.status, f.canonical_key), (MAPPED, "power_w"))

    def test_alias_conflict_is_flagged_and_lowers_confidence(self):
        (a,) = normalised(raw("Nischenbreite minimal", "560 mm"), raw("Hauteur minimum de la niche d'encastrement", "560 mm"))[:1]
        clean = map_attribute(a, category("oven"))[0]
        conflicted = map_attribute(replace(a, label_aliases=("Hauteur minimum de la niche d'encastrement",)), category("oven"))[0]
        self.assertEqual(conflicted.canonical_key, "niche_width_min_mm")
        self.assertLess(conflicted.confidence, clean.confidence)
        self.assertIn("niche_height_min_mm", " ".join(conflicted.reasons))


class NotASpec(unittest.TestCase):
    def check(self, label, value, reason, cat="unknown", section="", method="table_row"):
        f = one(label, value, cat, section, method=method)
        self.assertEqual((f.status, f.reasons), (NOT_A_SPEC, (reason,)), (label, value))

    def test_identity_variant_and_commercial_metadata(self):
        self.check("Штрихкод", "4650319702506", "identity_metadata")
        self.check("Страна производитель", "Китай", "identity_metadata")
        self.check("Türfarbe/-material", "Schwarz", "variant_metadata")
        self.check("Couleur de l'appareil", "Noir", "variant_metadata")
        self.check("FSA Eligible", "false", "commercial_metadata")
        self.check("Gebrauchsanweisungen", "[DE, ES]", "documentation_metadata")

    def test_legal_and_addresses(self):
        self.check("SRN", "NL-MF-000001693", "legal_or_certification", method="pdf_label_colon")
        self.check("En55011", "2016 + A1: 2017 + A11: 2020", "legal_or_certification", method="pdf_label_colon")
        self.check("дом", "65, каб. 519.", "company_or_address", method="pdf_tech_section_line")
        self.check("Хуаю Илектрикэл Эпплаинс Групп Ко, ЛТД.", "168 Хуаньчэн", "company_or_address", method="pdf_tech_section_line")
        self.check("условиях", "домах, офисах, квартирах, гостиницах", "document_fragment", method="pdf_label_colon")

    def test_promotional_chapter_content(self):
        chapter = "cs Chapter > cs Item"
        self.check("Plaque removal", "20 x more effective", "promotional_content", "oral_care", chapter, "json_state_pair")
        self.check("High", "To boost your clean", "promotional_content", "oral_care", chapter, "json_state_pair")
        self.check("Hoch", "Für eine noch bessere Reinigung", "promotional_content", "oral_care", chapter, "json_state_pair")

    def test_real_specs_in_a_promotional_group_survive(self):
        chapter = "cs Chapter > cs Item"
        self.assertEqual(one("Power supply", "100-240 V", "oral_care", chapter, method="json_state_pair").status, MAPPED)
        f = one("Energy consumption", "Standby without display <0.06 W", "oral_care", chapter, method="json_state_pair")
        self.assertEqual(f.status, UNMAPPED)                    # left for a later stage, but not marketing

    def test_mapped_specs_are_never_relabelled(self):
        f = one("Battery type", "Rechargeable Li-Po battery with a very long description of how nice it is to use", "oral_care",
                "cs Chapter > cs Item", method="json_state_pair")
        self.assertEqual(f.status, MAPPED)


class Collisions(unittest.TestCase):
    def fields(self, *pairs, cat="oven"):
        out = [f for label, value in pairs for f in rows(label, value, cat)]
        # rows() re-normalises per call, so attribute ids repeat: give each a distinct one
        return [replace(f, attribute=replace(f.attribute, id=f"n{i}")) for i, f in enumerate(out)]

    def test_same_value_equivalent_and_different(self):
        same = detect_collisions(self.fields(("Nettogewicht", "37.3 kg"), ("Poids net", "37.3 kg")))
        self.assertEqual([(c.canonical_key, c.kind, len(c.members)) for c in same], [("weight_kg", "same_value", 2)])
        equal = detect_collisions(self.fields(("Weight", "1000 g"), ("Weight", "1 kg")))
        self.assertEqual([c.kind for c in equal], ["equivalent_after_conversion"])
        differ = detect_collisions(self.fields(("Weight", "3 kg"), ("Gewicht", "4 kg")))
        self.assertEqual([c.kind for c in differ], ["different_values"])

    def test_collisions_are_kept_not_resolved(self):
        fields = self.fields(("Weight", "3 kg"), ("Gewicht", "4 kg"))
        collision = detect_collisions(fields)[0]
        self.assertEqual({m["normalized_value"] for m in collision.members}, {"3", "4"})
        self.assertEqual(len([f for f in fields if f.status == MAPPED and f.canonical_key == "weight_kg"]), 2)

    def test_list_fields_accumulate_instead_of_colliding(self):
        fields = self.fields(("Handle", "1 Prestige 9900"), ("Charger", "1 Charging base"), cat="oral_care")
        self.assertEqual([c.kind for c in detect_collisions(fields)], ["list_field"])

    def test_one_attribute_never_collides_with_itself(self):
        fields = rows("Abmessungen (H x B x T)", "1 x 2 x 3 mm", "oven")
        self.assertEqual(detect_collisions(fields), ())


class Categories(unittest.TestCase):
    def test_new_generic_categories(self):
        for category_id in ("oven", "power_tool", "oral_care", "coffee_machine", "computer_peripheral", "skincare"):
            self.assertIn(category_id, CATEGORY_NAMES)
        self.assertEqual(detect_category(product_texts=["Cordless hammer drill"]).category_id, "power_tool")
        self.assertEqual(detect_category(product_texts=["Sonic electric toothbrush"]).category_id, "oral_care")
        self.assertEqual(detect_category(product_texts=["Pump espresso coffee machines"]).category_id, "coffee_machine")
        self.assertEqual(detect_category(product_texts=["Niacinamide serum"]).category_id, "skincare")
        self.assertEqual(detect_category(product_texts=["Some unknown gadget"]).category_id, "unknown")

    def test_no_product_or_brand_rules_in_source(self):
        source = (ROOT / "core" / "canonical_mapping.py").read_text(encoding="utf-8").casefold()
        for banned in ("bosch", "makita", "dewalt", "philips", "sonicare", "delonghi", "logitech", "gressel", "ordinary", "hbg7741",
                       "dcd796", "dhp484", "hx9992", "ec685", "gaf-1825", "mx master", "lxt"):
            self.assertNotIn(banned, source)

    def test_every_concept_key_is_stable_and_unit_consistent(self):
        suffix_units = {"_mm": "mm", "_kg": "kg", "_w": "W", "_v": "V", "_hz": "Hz", "_a": "A", "_ah": "Ah", "_l": "L", "_m": "m",
                        "_rpm": "rpm", "_ipm": "ipm", "_nm": "Nm", "_bar": "bar", "_c": "°C", "_h": "h", "_dba": "dB(A)"}
        for c in CONCEPTS:
            self.assertRegex(c.key, r"^[a-z][a-z0-9_]*[a-z0-9]$")
            for suffix, unit in suffix_units.items():
                if c.key.endswith(suffix) and c.vtype in {"measure", "dimensions"}:
                    self.assertEqual(c.unit, unit, c.key)


class Saved(unittest.TestCase):
    """The eight saved Stage 35 results: mapping is offline, lossless and category-aware."""

    EXPECTED_CATEGORY = {
        "Gressel GAF-1825": "air_fryer", "Bosch HBG7741B1": "oven", "DEWALT DCD796P2": "power_tool",
        "Philips Sonicare 9900 Prestige HX9992/12": "oral_care", "Makita DHP484Z": "power_tool", "DeLonghi EC685M": "coffee_machine",
        "Logitech MX Master 3S": "computer_peripheral", "The Ordinary Niacinamide 10% + Zinc 1%": "skincare",
    }

    @classmethod
    def setUpClass(cls):
        directory = ROOT / "diagnostics" / "results" / "stage35"
        cls.results = {}
        cls.normalised = {}
        for product in PRODUCTS:
            path = directory / f"{slug(product)}-normalized.json"
            if not path.exists():
                raise unittest.SkipTest("Stage 35 traces not present")
            normalised = result_from_payload(json.loads(path.read_text(encoding="utf-8")))
            with network_forbidden():
                cls.results[normalised.product_name] = CanonicalMappingService().map(normalised)
            cls.normalised[normalised.product_name] = normalised

    def test_all_eight_offline_with_expected_categories(self):
        self.assertEqual(set(self.results), set(self.EXPECTED_CATEGORY))
        for name, result in self.results.items():
            self.assertEqual(result.category.category_id, self.EXPECTED_CATEGORY[name], name)
            self.assertEqual(result.network_calls, 0)

    def test_every_spec_attribute_is_accounted_for_and_nothing_else(self):
        for name, result in self.results.items():
            specs = {a.id for a in self.normalised[name].attributes if a.cls == SPEC}
            self.assertEqual(set(result.attribute_status()), specs, name)
            for f in result.fields:
                self.assertIn(f.status, (MAPPED, AMBIGUOUS, UNMAPPED, NOT_A_SPEC))
                self.assertEqual(f.attribute.cls, SPEC)

    def test_provenance_is_complete(self):
        for name, result in self.results.items():
            for f in result.fields:
                records = [e for e in f.evidence if e["kind"] == "source_record"]
                self.assertEqual(len(records), len(f.attribute.evidence), name)
                for record, source in zip(records, f.attribute.evidence):
                    self.assertEqual((record["raw_label"], record["raw_value"], record["source_url"], record["raw_index"]),
                                     (source.raw_label, source.raw_value, source.source_url, source.raw_index))
                if f.status == MAPPED:
                    self.assertTrue(f.canonical_key and f.canonical_label and f.mapping_method and f.category)
                    self.assertGreaterEqual(f.confidence, 0.70)
                    self.assertTrue([e for e in f.evidence if e["kind"] == "label" or e["kind"] == "group_path" or e["kind"] == "repeated_block"])
                if f.status in (AMBIGUOUS, UNMAPPED, NOT_A_SPEC):
                    self.assertEqual(f.canonical_key, "")
                if f.status == AMBIGUOUS:
                    self.assertTrue(f.candidates)

    def test_mapped_fields_are_unit_consistent(self):
        suffix = {"_mm": "mm", "_kg": "kg", "_w": "W", "_v": "V", "_hz": "Hz", "_l": "L", "_m": "m", "_rpm": "rpm", "_ipm": "ipm",
                  "_nm": "Nm", "_bar": "bar", "_c": "°C", "_h": "h", "_ah": "Ah", "_dba": "dB(A)", "_months": "month", "_years": "year"}
        for name, result in self.results.items():
            for f in result.mapped_fields():
                for tail, unit in suffix.items():
                    if f.canonical_key.endswith(tail):
                        self.assertEqual(f.unit, unit, (name, f.canonical_key))
                if f.unit_status == "implied":
                    self.assertLessEqual(f.confidence, 0.75)

    def test_ambiguous_are_not_promoted_and_low_confidence_never_mapped(self):
        for result in self.results.values():
            for f in result.fields:
                if f.status == AMBIGUOUS:
                    self.assertEqual(f.normalized_value, "")

    def test_marketing_and_metadata_do_not_pollute_canonical_fields(self):
        for name, result in self.results.items():
            mapped_labels = {f.attribute.label.casefold() for f in result.mapped_fields()}
            for banned in ("gum health", "plaque removal", "whitening", "sense iq", "штрихкод", "barcode", "sku", "mpn", "color", "farbe",
                           "couleur", "colore", "srn", "product type"):
                self.assertNotIn(banned, mapped_labels, name)
            for f in result.mapped_fields():
                self.assertFalse(f.canonical_key.startswith(("color", "colour", "sku", "gtin")), f.canonical_key)

    def test_expected_concepts_and_context(self):
        def keys(name):
            return {f.canonical_key: f for f in reversed(self.results[name].mapped_fields())}
        makita = keys("Makita DHP484Z")
        self.assertEqual(makita["drilling_capacity_steel_mm"].normalized_value, "13")
        self.assertEqual(makita["drilling_capacity_wood_mm"].normalized_value, "38")
        self.assertEqual(makita["battery_voltage_v"].normalized_value, "18")
        self.assertEqual(makita["no_load_speed_high_rpm"].normalized_value, "0–2000")
        self.assertEqual(makita["impact_rate_high_ipm"].normalized_value, "0–30000")
        self.assertNotIn("steel_mm", makita)
        dewalt = keys("DEWALT DCD796P2")
        self.assertEqual((dewalt["max_speed_rpm"].normalized_value, dewalt["height_mm"].normalized_value), ("2000", "202.9"))
        self.assertEqual(dewalt["impact_rate_ipm"].normalized_value, "34000")
        logi = self.results["Logitech MX Master 3S"]
        by_key = {}
        for f in logi.mapped_fields():
            by_key.setdefault(f.canonical_key, []).append(f.normalized_value)
        self.assertEqual(by_key["width_mm"], ["84.3"])
        self.assertEqual(by_key["secondary_component_width_mm"], ["14.4"])
        self.assertEqual(by_key["weight_kg"], ["0.141"])
        bosch = keys("Bosch HBG7741B1")
        self.assertEqual((bosch["height_mm"].normalized_value, bosch["width_mm"].normalized_value, bosch["depth_mm"].normalized_value),
                         ("595", "594", "548"))

    def test_multilingual_collapse_and_collisions_in_bosch(self):
        result = self.results["Bosch HBG7741B1"]
        by_key = {c.canonical_key: c for c in result.collisions}
        self.assertEqual(by_key["weight_kg"].kind, "same_value")
        self.assertEqual(len(by_key["weight_kg"].members), 2)       # Nettogewicht + "Weight net"
        self.assertIn("cooking_methods", by_key)                     # de / fr / it text values are kept side by side
        self.assertEqual(by_key["cooking_methods"].kind, "different_values")
        self.assertEqual(by_key["voltage_v"].kind, "same_value")
        philips = {c.canonical_key: c for c in self.results["Philips Sonicare 9900 Prestige HX9992/12"].collisions}
        self.assertEqual(philips["battery_runtime_h"].kind, "equivalent_after_conversion")
        self.assertEqual(philips["warranty_months"].kind, "equivalent_after_conversion")

    def test_coverage_and_counts_add_up(self):
        for name, result in self.results.items():
            summary = result.summary()
            self.assertEqual(summary["mapped"] + summary["ambiguous"] + summary["unmapped"] + summary["not_a_spec"], summary["normalized_specs"])
            self.assertEqual(summary["eligible"], summary["normalized_specs"] - summary["not_a_spec"])
            self.assertGreaterEqual(summary["coverage"], 0.60, name)
            self.assertGreater(summary["unique_canonical_fields"], 5, name)

    def test_source_modules_stay_offline(self):
        import core.canonical_mapping
        import services.canonical_mapping
        for module in (core.canonical_mapping, services.canonical_mapping):
            source = Path(module.__file__).read_text(encoding="utf-8")
            for banned in ("requests", "urllib.request", "playwright", "core.discovery", "services.discovery", "socket"):
                self.assertNotIn(f"import {banned}", source)
                self.assertNotIn(f"from {banned}", source)


if __name__ == "__main__":
    unittest.main()
