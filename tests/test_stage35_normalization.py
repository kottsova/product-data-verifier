"""Stage 35: normalisation of raw attributes (offline, generic)."""

from __future__ import annotations

import json
from pathlib import Path
import unittest

from core.normalization import (
    CONTENT, IDENTITY, SPEC, UNKNOWN, normalize_attributes, normalize_label, normalize_text,
    normalize_value, parse_measure, qualified_label,
)
from core.raw_extraction import RawAttribute
from diagnostics.stage34_extraction import PRODUCTS, slug
from diagnostics.stage35_normalization import extraction_from_payload, network_forbidden
from services.normalization import NormalizationService

ROOT = Path(__file__).resolve().parents[1]
URL = "https://brand.example/en/p/abc1234x"


def raw(label, value, method="table_row", section="", url=URL, location="visible_dom"):
    return RawAttribute(label, value, url, "official_product_page", location, method, section)


def run(*records, model="ABC1234X"):
    return normalize_attributes(list(records), model)


class Values(unittest.TestCase):
    def test_duplicated_unit_and_range(self):
        m = parse_measure("50-60 HzHz")
        self.assertEqual((m.value, m.unit), ("50–60", "Hz"))
        self.assertEqual(parse_measure("50-60 Hz Hz").unit, "Hz")
        self.assertEqual(parse_measure("15 mm").unit, "mm")  # "mm" is a unit, not a doubled "m"

    def test_unit_glued_or_dashed_and_separators(self):
        self.assertEqual(parse_measure("2000-rpm").display, "2000 rpm")
        self.assertEqual(parse_measure("0-2,000rpm").display, "0–2000 rpm")
        self.assertEqual(parse_measure("12,6 мб").value, "12.6")
        self.assertEqual(parse_measure("149x330x305").value, "149 × 330 × 305")

    def test_dual_measure_keeps_both(self):
        m = parse_measure("3.32 in (84.3 mm)")
        self.assertEqual((m.value, m.unit, m.alternates), ("3.32", "in", ("84.3 mm",)))

    def test_identifiers_are_not_ranges(self):
        self.assertIsNone(parse_measure("910-007500"))
        attrs, _, _ = run(raw("Part", "910-007500", "json_ld_field"))
        self.assertEqual(attrs[0].value, "910-007500")

    def test_booleans_and_i18n(self):
        for text, flag in (("Yes", True), ("Nein", False), ("нет", False), ("specifications.translatedBoolean.no", False),
                           ("specifications.translatedBoolean.yes", True), ("true", True)):
            self.assertIs(normalize_value(text).boolean, flag, text)
        self.assertEqual(normalize_value("specifications.foo.barBaz").kind, "i18n_key")

    def test_placeholders_and_template_text(self):
        for text in ("", ",", "-", "N/A", "Nicht zutreffend"):
            self.assertEqual(normalize_value(text).kind, "placeholder", text)
        self.assertEqual(normalize_value("Lorem Ipsum").kind, "template_text")

    def test_text_hygiene(self):
        self.assertEqual(normalize_text("Tamper\\u002Fx &amp; y\u00a0z"), "Tamper/x & y z")
        self.assertEqual(normalize_text("a–b"), "a-b")
        self.assertEqual(normalize_value("Sensors ,, dual ;").text, "Sensors, dual")

    def test_locale_prefixes(self):
        self.assertEqual(normalize_value("en: Remote, en: Monitoring").text, "Remote, Monitoring")


class Labels(unittest.TestCase):
    def test_nested_bullet_child_keeps_group(self):
        label = normalize_label("- Steel", "Specifications > Drilling capacity")
        self.assertEqual((label.label, qualified_label(label)), ("Steel", "Drilling capacity > Steel"))

    def test_label_unit_moves_to_unit(self):
        attrs, _, _ = run(raw("Power (W)", "1450"), raw("Cord, cm", "70"))
        self.assertEqual([(a.label, a.unit) for a in attrs], [("Power", "W"), ("Cord", "cm")])

    def test_label_unit_kept_when_value_is_text(self):
        attrs, _, _ = run(raw("Mode (W)", "eco"))
        self.assertEqual(attrs[0].label, "Mode (W)")

    def test_identifier_style_labels(self):
        self.assertEqual(normalize_label("TEMP_RANGE").label, "Temp range")
        self.assertEqual(normalize_label("Filters*:").label, "Filters")


class Classification(unittest.TestCase):
    def test_json_ld_service_fields_are_not_specs(self):
        attrs, _, _ = run(
            raw("name", "Widget", "json_ld_field", location="json_ld"), raw("sku", "X1", "json_ld_field", location="json_ld"),
            raw("gtin13", "4006381333931", "json_ld_field", location="json_ld"), raw("weight", "2 kg", "json_ld_field", location="json_ld"),
            raw("Barcode", "5035050000000", "json_ld_property", location="json_ld"),
        )
        self.assertEqual([a.cls for a in attrs], [IDENTITY, IDENTITY, IDENTITY, SPEC, IDENTITY])

    def test_spec_marketing_unknown(self):
        attrs, _, _ = run(
            raw("Voltage", "18V"),
            raw("Why it works", "Our revolutionary formula gently massages the skin and leaves it feeling soft and fresh all day long. Try it.", "element_pair"),
            raw("Random", "thing", "element_pair"),
            raw("Model", "ABC1234X"),
        )
        self.assertEqual([a.cls for a in attrs], [SPEC, CONTENT, UNKNOWN, IDENTITY])

    def test_template_block_is_not_product_data(self):
        attrs, _, _ = run(
            raw("Client Name", "Lorem Ipsum", "element_pair", "Invoice"), raw("Email", "a@b.c", "element_pair", "Invoice"),
        )
        self.assertEqual({a.cls for a in attrs}, {UNKNOWN})

    def test_variant_nodes_stay_out_of_specs(self):
        ld = dict(method="json_ld_field", location="json_ld")
        attrs, nodes, _ = run(
            raw("name", "Mouse", **ld), raw("brand", "Acme", **ld),
            raw("name", "Mouse - Black", **ld), raw("sku", "910-1", **ld), raw("color", "Black", **ld),
            raw("name", "Mouse - Graphite", **ld), raw("sku", "910-2", **ld), raw("color", "Graphite", **ld),
            raw("Weight", "141 g"),
        )
        self.assertEqual([n.role for n in nodes], ["parent", "variant", "variant"])
        self.assertEqual({a.cls for a in attrs if a.label in {"sku", "color", "name"}}, {IDENTITY})
        self.assertEqual([a.value for a in attrs if a.label == "sku"], ["910-1", "910-2"])
        self.assertEqual({a.variant for a in attrs if a.label == "sku"}, {"Black", "Graphite"})
        self.assertEqual([a.cls for a in attrs if a.label == "Weight"], [SPEC])


class Deduplication(unittest.TestCase):
    def test_same_label_value_across_locations_merges_and_keeps_evidence(self):
        attrs, _, _ = run(
            raw("Voltage", "18V", "json_ld_property", location="json_ld"),
            raw("voltage", "18 V", "json_state_pair", location="json_state"),
            raw("Voltage", "18V", "table_row", location="dom_hidden"),
        )
        self.assertEqual(len(attrs), 1)
        self.assertEqual(len(attrs[0].evidence), 3)
        self.assertEqual(set(attrs[0].locations), {"json_ld", "json_state", "dom_hidden"})
        self.assertEqual(set(attrs[0].methods), {"json_ld_property", "json_state_pair", "table_row"})

    def test_missing_unit_is_compatible_but_different_unit_is_not(self):
        attrs, _, _ = run(raw("Length", "70 cm"), raw("Length", "70"))
        self.assertEqual([(a.unit, len(a.evidence)) for a in attrs], [("cm", 2)])
        attrs, _, _ = run(raw("Length", "70 cm"), raw("Length", "70 mm"))
        self.assertEqual(sorted((a.unit, len(a.evidence)) for a in attrs), [("cm", 1), ("mm", 1)])

    def test_really_different_values_are_not_merged(self):
        attrs, _, _ = run(raw("Width", "3.32 in"), raw("Width", "0.57 in"))
        self.assertEqual(len(attrs), 2)

    def test_nested_children_are_compared_inside_their_group(self):
        attrs, _, _ = run(
            raw("- Hi", "0-2,000rpm", section="Specs > No load speed"), raw("- Hi", "0-2,000rpm", section="Specs > Impacts"),
        )
        self.assertEqual(len(attrs), 2)

    def test_sibling_locales_align_by_value_order(self):
        de = "https://brand.example/de/p/abc"
        it = "https://brand.example/it/p/abc"
        values = ["37.3", "120 cm", "220-240 V", "3600 W", "71 L"]
        records = [raw(f"Gewicht{n}", v, "json_ld_property", url=de) for n, v in enumerate(values)]
        records += [raw(f"Peso{n}", v, "json_ld_property", url=it) for n, v in enumerate(values)]
        attrs, _, _ = run(*records)
        self.assertEqual(len(attrs), 5)
        self.assertTrue(all(len(a.evidence) == 2 and a.label_aliases for a in attrs))

    def test_every_raw_record_is_kept_exactly_once(self):
        records = [raw("Voltage", "18V"), raw("Voltage", "18 V", "json_state_pair"), raw("x", ""), raw("name", "W", "json_ld_field")]
        attrs, _, _ = run(*records)
        indices = sorted(e.raw_index for a in attrs for e in a.evidence)
        self.assertEqual(indices, list(range(len(records))))
        for a in attrs:
            for e in a.evidence:
                self.assertEqual((e.raw_label, e.raw_value), (records[e.raw_index].raw_label, records[e.raw_index].raw_value))


class Provenance(unittest.TestCase):
    def test_attribute_carries_full_provenance(self):
        attrs, _, _ = run(raw("Voltage", "18V", "json_ld_property", location="json_ld"))
        a = attrs[0]
        self.assertEqual((a.label, a.value, a.unit, a.cls), ("Voltage", "18", "V", SPEC))
        self.assertEqual((a.source_urls, a.source_types, a.locations, a.methods),
                         ((URL,), ("official_product_page",), ("json_ld",), ("json_ld_property",)))
        self.assertEqual(a.evidence[0].raw_value, "18V")


class Saved(unittest.TestCase):
    """The eight Stage 34 extractions (saved traces): normalisation is offline and lossless."""

    def test_no_network_and_no_discovery_imports(self):
        import core.normalization
        import services.normalization
        for module in (core.normalization, services.normalization):
            source = Path(module.__file__).read_text(encoding="utf-8")
            for banned in ("requests", "urllib.request", "playwright", "core.discovery", "services.discovery"):
                self.assertNotIn(f"import {banned}", source)
                self.assertNotIn(f"from {banned}", source)

    def test_eight_products(self):
        directory = ROOT / "diagnostics" / "results" / "stage34"
        files = [directory / f"{slug(p)}-extraction.json" for p in PRODUCTS]
        if not all(f.exists() for f in files):
            self.skipTest("Stage 34 traces not present")
        for path in files:
            extraction = extraction_from_payload(json.loads(path.read_text(encoding="utf-8")))
            with network_forbidden():
                result = NormalizationService().normalize(extraction)
            self.assertEqual(result.network_calls, 0)
            self.assertEqual(sum(len(a.evidence) for a in result.attributes), result.raw_count, path.name)
            self.assertEqual(result.raw_count, len(extraction.attributes))
            self.assertLessEqual(len(result.of_class(SPEC)), result.raw_by_class[SPEC])
            for a in result.of_class(SPEC):
                self.assertNotIn(a.label_key, {"sku", "mpn", "gtin", "name", "brand"})
                self.assertNotRegex(a.display, r"translatedBoolean|HzHz")
            for a in result.attributes:
                self.assertTrue(a.evidence and a.source_urls and a.methods and a.locations)


if __name__ == "__main__":
    unittest.main()
