"""Stage 34: raw attribute extraction from a DiscoveryDebugResult (offline fixtures)."""

from __future__ import annotations

import json
from pathlib import Path
import unittest
from unittest import mock

from core.raw_extraction import (
    extract_document_attributes,
    extract_html_attributes,
    scope_document_pages,
    sibling_identifier,
)
from diagnostics.stage34_extraction import discovery_from_payload, searching_forbidden
from services.discovery_debug import DiscoveryDebugService
from services.raw_extraction import RawExtractionService

ROOT = Path(__file__).resolve().parents[1]
MODEL = "ABC1234X"


def pairs(html: str, model: str = MODEL, **kw):
    result, stats = extract_html_attributes(html, "https://brand.example/p/abc1234x", model, **kw)
    return {(a.raw_label, a.raw_value): a for a in result.attributes}, result, stats


class HtmlReaders(unittest.TestCase):
    def test_visible_and_hidden_tables_keep_distinct_locations(self):
        html = """
        <table><tr><th>Weight</th><td>1.6 kg</td></tr></table>
        <div class="tab-pane"><table><tr><td>Voltage</td><td>18 V</td></tr></table></div>
        <details><summary>More</summary><dl><dt>Torque</dt><dd>60 Nm</dd></dl></details>
        <template><table><tr><td>Speed</td><td>500 rpm</td></tr></table></template>"""
        found, _, _ = pairs(html)
        self.assertEqual(found[("Weight", "1.6 kg")].location, "visible_dom")
        self.assertEqual(found[("Voltage", "18 V")].location, "dom_hidden")
        self.assertEqual(found[("Torque", "60 Nm")].location, "dom_hidden")
        self.assertEqual(found[("Torque", "60 Nm")].method, "definition_list")
        self.assertEqual(found[("Speed", "500 rpm")].location, "template")

    def test_json_ld_product_properties_and_provenance(self):
        payload = {"@type": "Product", "sku": MODEL, "brand": {"name": "Acme"},
                   "additionalProperty": [{"@type": "PropertyValue", "name": "Voltage", "value": "18", "unitText": "V"}]}
        found, _, _ = pairs(f'<script type="application/ld+json">{json.dumps(payload)}</script>')
        attribute = found[("Voltage", "18 V")]
        self.assertEqual((attribute.location, attribute.method), ("json_ld", "json_ld_property"))
        self.assertEqual(attribute.source_url, "https://brand.example/p/abc1234x")
        self.assertEqual(attribute.source_type, "official_product_page")

    def test_js_object_literal_state_and_next_flight_payload(self):
        literal = ('<script>window.__S={inRiverTechSpecs:{sensor:[{facet:"DPI",values:[{value:"200-8000",variants:[]}]}],'
                   '__proto__:null},techSpecs:{}};</script>')
        found, _, _ = pairs(literal)
        self.assertEqual(found[("DPI", "200-8000")].location, "json_state")
        inner = json.dumps({"specification": {"csChapter": [{"csItem": [
            {"csItemName": "Battery", "csValue": [{"csValueName": "Lithium ION"}]}]}]}})
        push = json.dumps(f'1:{inner}')
        flight = f'<script>self.__next_f.push([1,{push}])</script>'
        found, _, _ = pairs(flight)
        self.assertEqual(found[("Battery", "Lithium ION")].location, "json_state")

    def test_keyed_spec_list_resolved_against_same_record_with_labels(self):
        state = {
            "tech_spec_pumpPressureBar": {"defaultMessage": "Pump pressure (bar)"},
            "product": {"c_pumpPressureBar": "15", "c_groupedTechSpecs": {"grp_technicaldata": ["pumpPressureBar"]}},
        }
        found, _, _ = pairs(f'<script type="application/json">{json.dumps(state)}</script>')
        attribute = found[("Pump pressure (bar)", "15")]
        self.assertEqual((attribute.method, attribute.section), ("json_state_keyed_spec", "grp_technicaldata"))

    def test_neighbour_sku_never_mixed_in(self):
        related = '<div class="related-products"><dl><dt>Weight</dt><dd>9 kg</dd></dl></div>'
        columns = ("<table><thead><tr><th></th><th>ABC1234Y</th><th>ABC9999Z</th></tr></thead>"
                   "<tr><td>Power</td><td>100 W</td><td>200 W</td></tr></table>")
        state = json.dumps({"specifications": [{"sku": "ABC1234Y", "name": "Power", "value": "300 W"}]})
        html = f'{related}{columns}<script type="application/json">{state}</script>'
        found, result, _ = pairs(html)
        self.assertEqual(found, {})
        self.assertGreaterEqual(sum(v for k, v in result.excluded.items() if k.startswith("neighbour")), 3)

    def test_model_column_selected_in_multi_model_table(self):
        html = ("<table><thead><tr><th></th><th>ABC1234X</th><th>ABC1234Y</th></tr></thead>"
                "<tr><td>Power</td><td>100 W</td><td>200 W</td></tr></table>")
        found, _, _ = pairs(html)
        self.assertEqual(set(found), {("Power", "100 W")})

    def test_sibling_identifier(self):
        self.assertTrue(sibling_identifier("DHP484Z", "kit DHP483Z"))
        self.assertTrue(sibling_identifier("EC685M", "EC685 EC695 EC785"))
        self.assertEqual(sibling_identifier("DHP484Z", "DHP484Z and EN 60335"), "")


class DocumentScoping(unittest.TestCase):
    def test_model_page_used_shared_page_excluded(self):
        pages = [
            "Technical data\nVoltage: 18 V\nModel ABC1234X",
            "Models ABC1234X ABC1234Y\nTechnical data\nWeight: 9 kg",
            "Other chapter\nNote: nothing",
        ]
        selected, report = scope_document_pages(pages, MODEL)
        self.assertEqual([number for number, _, _ in selected], [1])
        self.assertTrue(report["multi_model_document"])
        result, _ = extract_document_attributes(pages, "https://x/doc.pdf", MODEL, "datasheet", "exact")
        self.assertEqual({a.raw_label for a in result.attributes}, {"Voltage"})
        self.assertEqual(result.excluded["neighbour_sku_shared_page"], 1)
        self.assertEqual(result.attributes[0].location, "document")
        self.assertEqual(result.attributes[0].page_ref, "1")

    def test_document_that_never_names_model_is_withheld(self):
        result, report = extract_document_attributes(
            ["Technical data\nVoltage: 18 V"], "https://x/doc.pdf", MODEL, "datasheet", "exact",
        )
        self.assertEqual(result.attributes, [])
        self.assertIn("document_model_not_in_text", result.excluded)
        self.assertFalse(report["model_in_text"])

    def test_manual_prose_is_not_an_attribute(self):
        text = "ABC1234X user manual\nNote: keep away from water.\nSee: page 3\nTechnical data\nVoltage: 18 V"
        result, _ = extract_document_attributes([text], "https://x/m.pdf", MODEL, "manual", "exact")
        self.assertEqual({a.raw_label for a in result.attributes}, {"Voltage"})


def _payload() -> dict:
    src = lambda url, match, role="product": {  # noqa: E731
        "url": url, "domain": "brand.example", "source_type": "manufacturer", "title": "t", "model_match": match,
        "authority": "manufacturer", "reason": "", "group": "official", "page_role": role,
    }
    return {
        "product_name": "Acme ABC1234X", "brand": "Acme", "model": MODEL, "market": "", "status": "PASS",
        "exact_official_found": True,
        "official": [],
        "official_pages": [src("https://brand.example/p/exact", "exact"), src("https://brand.example/p/weak", "weak")],
        "support_pages": [src("https://brand.example/support/x", "probable", "support")],
        "documents": [{
            "url": "https://brand.example/doc.pdf", "doc_type": "datasheet", "canonical_field": "datasheet",
            "title": "ABC1234X datasheet", "authority": "official", "model_match": "exact", "reason": "",
            "file_type": "pdf", "found_on": [], "locales": [],
        }],
    }


class ServiceUsesDiscovery(unittest.TestCase):
    def setUp(self):
        self.fetched: list[str] = []
        html = "<h1>ABC1234X</h1><table><tr><td>Voltage</td><td>18 V</td></tr></table>"

        def fetch_html(url):
            self.fetched.append(url)
            return url, html

        def fetch_bytes(url):
            self.fetched.append(url)
            return None

        patcher = mock.patch("services.raw_extraction.time.sleep")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.service = RawExtractionService(fetch_html=fetch_html, fetch_bytes=fetch_bytes)

    def test_only_discovered_official_sources_are_fetched_and_nothing_is_searched(self):
        discovery = discovery_from_payload(_payload())
        with searching_forbidden():
            result = self.service.extract(discovery)
        self.assertEqual(result.search_calls, 0)
        self.assertEqual(sorted(set(self.fetched)), [
            "https://brand.example/doc.pdf", "https://brand.example/p/exact", "https://brand.example/support/x",
        ])
        self.assertEqual([s.url for s in result.pages], ["https://brand.example/p/exact"])
        self.assertEqual([s.url for s in result.support_pages], ["https://brand.example/support/x"])
        self.assertEqual({item["url"] for item in result.skipped}, {"https://brand.example/p/weak"})
        self.assertEqual(len(result.attributes), 2)
        self.assertEqual({a.source_type for a in result.attributes}, {"official_product_page", "official_support_page"})
        self.assertEqual(result.documents[0].issues, ("fetch_failed",))  # separate, and honest about it

    def test_searching_guard_trips_if_discovery_is_called(self):
        with searching_forbidden():
            with self.assertRaises(AssertionError):
                DiscoveryDebugService().discover_name("Acme ABC1234X")

    def test_extraction_modules_do_not_reference_search_entry_points(self):
        for name in ("core/raw_extraction.py", "services/raw_extraction.py"):
            source = (ROOT / name).read_text(encoding="utf-8")
            for forbidden in ("discover_with_status", "DiscoveryDebugService", "ResilientSearchSession", "search_query"):
                self.assertNotIn(forbidden, source, f"{name} must not search ({forbidden})")
        self.assertNotIn("language", (ROOT / "core/raw_extraction.py").read_text(encoding="utf-8").lower())

    def test_unfetchable_page_is_reported_not_hidden(self):
        service = RawExtractionService(fetch_html=lambda url: None, fetch_bytes=lambda url: None)
        with mock.patch("services.raw_extraction.time.sleep"):
            result = service.extract(discovery_from_payload(_payload()))
        self.assertEqual(result.pages[0].issues, ("fetch_failed",))
        self.assertEqual(result.attributes, ())


if __name__ == "__main__":
    unittest.main()
