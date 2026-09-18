"""Stage 32 regression: full-spec atomic extraction, dynamic schema, EN/RU parity.

Deterministic, no network, and deliberately *not* tied to any real product:
the fixture is an invented side-by-side page for "Acme Phone 5" and
"Acme Phone 5 Max" that reproduces the structure of a real official spec table
(column headers, section rows, bold sub-headings, wrapped values, inline
footnote numbers, link fragments).
"""

from __future__ import annotations

import csv
from io import StringIO
from pathlib import Path
import re
import unittest

from bot.export import build_wide_export_row, export_result_csv
from bot.formatters import (
    format_auxiliary,
    format_result,
    found_counts,
    ordered_sources,
    product_image_records,
)
from bot.i18n import ATTRIBUTE_DISPLAY_NAMES, display_name
from core.attribute_catalog import catalog_canonicals, resolve_canonical
from core.discovery import DiscoveryOutcome
from core.extract import extract_attributes
from core.identity import resolve_product_identity
from core.official_atomic import (
    Line,
    expand_bands,
    is_heading,
    merge_continuations,
    read_section,
)
from core.official_spec_table import completeness_diff, extract_official_section_specs
from core.schema import get_attribute_schema
from core.workflow import ProductWorkflowRequest, WorkflowServices, run_product_workflow
from services.product_verifier import VerifyProductRequest, _to_result
from tests.test_workflow import candidate, fetch_result

SPECS_URL = "https://fi.acme.example/about/phones/acme-phone-5-specs"
SECONDARY_URL = "https://www.gsm.example/acme_phone_5-1.php"


def _line(spec) -> str:
    if isinstance(spec, tuple):
        kind, text = spec
        if kind == "b":
            return f"<div><b>{text}</b></div>"
        if kind == "fn":  # inline footnote number followed by a marker
            return (f'<div>{text}<span data-component-name="superscript">'
                    f'<a href="#footnote:x"><sup></sup></a></span></div>')
    return f"<div>{spec}</div>"


COMMON = {
    "Security": ["Encrypted storage", "Secure boot", "Learn more at", "acme.example/security", "and"],
    "Authentication": ["Fingerprint unlock", "Face unlock"],
    "OS": ["Launched with Zeta OS 3"],
    "Ports": ["USB Type-C 3.1", "Power button", "Volume controls"],
    "Media": ["Stereo speakers", "2 microphones", "Spatial audio"],
    "Wireless & Location": [
        "Wi-Fi 6E (802.11ax) with 2.4GHz+5GHz+6GHz, 2x2 MIMO",
        "Bluetooth v5.2 with dual antennas", "NFC", "Dual GNSS", "GPS, GLONASS, Galileo",
        "Dual SIM (Nano SIM and eSIM)",
    ],
    "Haptics": ["Haptic engine: Linear resonant", "Haptic levels: 3"],
    "Colors": ["Graphite", "Sand", "Mint"],
    "In the Box": ["1 m USB-C cable", "SIM tool"],
    "Safety Information": ["Regulatory & safety documentation"],
}

SECTIONS = [
    ("Display", (
        ["6.1-inch Vivid display", "(LTPO)", "1200 x 2600 LTPO OLED at 460 PPI",
         "19.5:9 aspect ratio", "Adaptive refresh (10-120Hz)", "Ceramic Shield cover glass",
         "Up to 1800 nits (HDR) and up to 2600 nits (peak brightness)",
         ">1,000,000:1 contrast ratio", "HDR support", "Full 30-bit depth for 1 billion colors"],
        ["6.7-inch Vivid display", "(LTPO)", "1400 x 3000 LTPO OLED at 440 PPI",
         "19.5:9 aspect ratio", "Adaptive refresh (10-120Hz)", "Ceramic Shield cover glass",
         "Up to 1800 nits (HDR) and up to 2900 nits (peak brightness)",
         ">1,000,000:1 contrast ratio", "HDR support", "Full 30-bit depth for 1 billion colors"],
    )),
    ("Dimensions and Weight", (
        ["5.9 height x 2.8 width x 0.3 depth (inches)", "150.0 height x 71.0 width x 8.0 depth (mm)",
         "6.6 oz", "187 g"],
        ["6.4 height x 3.0 width x 0.3 depth (inches)", "163.0 height x 77.0 width x 8.0 depth (mm)",
         "7.8 oz", "221 g"],
    )),
    ("Battery and Charging", (
        ["Typical 4400 mAh (Minimum 4300 mAh)", "Fast charging – up to 50% in about 25 minutes",
         "– using 30W charger, sold separately", "Fast wireless charging (Qi-certified)",
         "30+ hour battery life", "Power Share"],
        ["Typical 5000 mAh (Minimum 4900 mAh)", "Fast charging – up to 60% in about 25 minutes",
         "– using 30W charger, sold separately", "Fast wireless charging (Qi-certified)",
         "40+ hour battery life", "Power Share"],
    )),
    ("Memory and Storage", (["8 GB RAM", "128 GB / 256 GB"], ["12 GB RAM", "128 GB / 256 GB / 512 GB"])),
    ("Processors", (["Acme Silicon A2", "Guard M1 security coprocessor"],) * 2),
    ("Rear Camera", (
        [("b", "Triple rear camera system: 48 MP wide | 12 MP ultrawide | 12 MP 3x telephoto lens"),
         "48 MP Octa PD wide camera", "ƒ/1.7 aperture", "80° field of view",
         "1/1.3\" image sensor size", "12 MP Quad PD ultrawide camera with autofocus",
         "ƒ/2.2 aperture", "120° field of view", "12 MP Quad PD telephoto camera",
         "ƒ/2.8 aperture", "3x optical zoom", "Zoom up to 20x",
         "Laser autofocus sensor"],
    ) * 2),
    ("Front Camera", (["24 MP Dual PD selfie camera with autofocus", "ƒ/2.0 aperture",
                       "95° ultrawide field of view"],) * 2),
    ("Video", ([
        ("b", "Rear Camera"), "8K video recording at 30 FPS", "4K video recording at 24/30/60 FPS",
        ("fn", "Dual exposure on wide camera12"),
        ("b", "Front Camera"), "4K video recording at 30/60 FPS",
        ("b", "Video Features"), "Night video", "Slo-mo video support up to 240 FPS",
        "10-bit HDR video", "Video formats: HEVC (H.265), AVC (H.264)",
        ("b", "Audio"), "Stereo recording", "Wind noise reduction",
    ],) * 2),
    ("Materials and durability", ([
        "Ceramic Shield cover glass", "IP68 dust and water resistance",
        "Aluminium frame with matte glass back", "Made with 20% recycled materials",
    ],) * 2),
    ("Security and OS updates", (["5 years of OS, security and feature updates"],) * 2),
    ("5G", (
        ["5G mmWave + Sub 6GHz", "Model AX1", "GSM/EDGE: Quad-band (850, 900, 1800, 1900 MHz)",
         "UMTS/HSPA+/HSDPA: Bands 1,2,4,5,8", "LTE: Bands B1/2/3/4/5/7/8/12/13/14/17/18/19/20/25/26/28/29/30/38/40/41/48",
         "/66/71", ("fn", "5G Sub-6"), ": Bands n1/2/3/5/7/8/12/14/20/25/26/28/29/30/38/40/41/48/66/70/71/77", "/78",
         "5G mmWave", ": Bands n258/260/261", "eSIM"],
        ["5G mmWave + Sub 6GHz", "Model AX2", "GSM/EDGE: Quad-band (850, 900, 1800, 1900 MHz)",
         "UMTS/HSPA+/HSDPA: Bands 1,2,4,5,8", "LTE: Bands B1/2/3/4/5/7/8", ("fn", "5G Sub-6"), ": Bands n1/2/3/5/7/8",
         "5G mmWave", ": Bands n258/260/261", "eSIM"],
    )),
    ("Accessibility", ([
        "Hearing aid-compatible per FCC requirements.",
        "Conversational Gain: 14 dB w/ aid & 18 dB w/o aid (DA 23-914). See", "acme.example/hac", ".",
        "Magnifier", "Live captions",
    ],) * 2),
] + [(heading, (lines,) * 2) for heading, lines in COMMON.items()]


def specs_html(columns=("Acme Phone 5", "Acme Phone 5 Max"), sections=SECTIONS) -> str:
    rows = ["<tr>" + "".join(f'<th scope="col" id="c{i}">{name}</th>' for i, name in enumerate(columns)) + "</tr>"]
    for index, (heading, cells) in enumerate(sections):
        rows.append(f'<tr><th scope="colgroup" id="s{index}">{heading}</th><th scope="colgroup">{heading}</th></tr>')
        rows.append("<tr>" + "".join(
            f'<td headers="c{i} s{index}">' + "".join(_line(line) for line in cells[i if i < len(cells) else 0]) + "</td>"
            for i in range(len(columns))
        ) + "</tr>")
    return ("<html><head><title>Acme Phone 5 | Tech Specs</title></head><body><h1>Acme Phone 5</h1>"
            "<table><tbody>" + "".join(rows) + "</tbody></table></body></html>")


GSM_HTML = (
    '<html><body><a href="acme_phone_5-review-1.php">Review</a>'
    '<a href="acme_phone_5-reviews-1.php">Opinions</a><a href="compare.php3?id=1">Compare</a>'
    '<a href="acme_phone_5-pictures-1.php">Pictures</a><a href="acme_phone_5-price-1.php">Prices</a>'
    '<a href="related.php3?id=1">Related devices</a>'
    "<table><tr><th>GPU</th><td>Acme G-Core 9</td></tr></table></body></html>"
)


def official_candidate(url):
    item = candidate(url, title="Acme Phone 5", relation="same_base_model")
    item["authority_evidence_url"] = "https://fi.acme.example/"
    return item


def secondary_candidate():
    item = candidate(SECONDARY_URL, title="Acme Phone 5", source_type="specialized_reference",
                     authority="unknown", relation="same_base_model", score=80)
    item["authority_evidence_url"] = SECONDARY_URL
    return item


def run_page(pages, initial, model="Acme Phone 5"):
    def fetch(item):
        source = fetch_result(item)
        source["html"] = pages[item["url"]]
        source["text"] = ""
        return source

    outcome = DiscoveryOutcome(list(initial), "success", ["initial"], ["initial"], [])
    services = WorkflowServices(
        lambda identity, market: outcome,
        lambda identity, query, market: DiscoveryOutcome([], "success", [query], [query], []),
        fetch, extract_attributes,
    )
    return run_product_workflow(
        ProductWorkflowRequest(f"Acme {model.removeprefix('Acme ')}", brand="Acme", targeted_search_enabled=False),
        services=services,
    )


def spec_source(html=None):
    return {
        "status": "success", "html": html or specs_html(), "final_url": SPECS_URL, "source_url": SPECS_URL,
        "source_type": "manufacturer", "authority_status": "verified",
        "model_relevance": "exact_base_model", "identity_relation": "same_base_model", "document_type": "html",
    }


def atomic(model="Acme Phone 5"):
    attrs, diagnostics = extract_official_section_specs(spec_source(), resolve_product_identity(model, "Acme"))
    return attrs, diagnostics, {f["canonical"]: f for f in diagnostics["facts"]}


class ParentSectionDecompositionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.attrs, cls.diagnostics, cls.facts = atomic()

    def value(self, canonical):
        return self.facts[canonical]["value"]

    def test_display_parent_becomes_many_single_value_attributes(self):
        expected = {
            "display_size": "6.1", "display_type": "LTPO OLED", "display_resolution": "1200 x 2600",
            "display_ppi": "460", "display_aspect_ratio": "19.5:9", "refresh_rate": "10-120",
            "cover_glass": "Ceramic Shield", "hdr_support": "Yes", "hdr_brightness": "1800",
            "peak_brightness": "2600", "contrast_ratio": ">1,000,000:1", "color_depth": "30-bit",
            "number_of_colors": "1 billion",
        }
        for canonical, value in expected.items():
            self.assertEqual(self.value(canonical), value, canonical)

    def test_one_cell_one_characteristic(self):
        for fact in self.diagnostics["facts"]:
            if fact["canonical"].endswith(("_features", "authentication", "sensors", "color",
                                           "package_contents", "materials_and_durability")):
                continue  # explicit list attributes
            self.assertNotIn(" LTPO ", f" {fact['value']} " if fact["canonical"] != "display_type" else "")
        # the display cell did not collapse into one "Display" attribute
        self.assertNotIn("display", self.facts)

    def test_battery_parent(self):
        self.assertEqual(self.value("battery_capacity"), "4400")
        self.assertEqual(self.value("battery_capacity_min"), "4300")
        self.assertEqual(self.value("charging_power"), "30")
        self.assertEqual(self.value("fast_charging"), "up to 50% in about 25 minutes")
        self.assertEqual(self.value("wireless_charging"), "Yes")
        self.assertEqual(self.value("wireless_charging_standard"), "Qi")
        self.assertEqual(self.value("battery_life"), "30+")

    def test_camera_parents_are_split_per_lens_and_side(self):
        self.assertEqual(self.value("rear_camera"), "48 MP wide + 12 MP ultrawide + 12 MP telephoto")
        self.assertEqual(self.value("wide_camera_aperture"), "ƒ/1.7")
        self.assertEqual(self.value("ultrawide_camera_field_of_view"), "120")
        self.assertEqual(self.value("telephoto_camera_optical_zoom"), "3x")
        self.assertEqual(self.value("max_zoom"), "20x")
        self.assertEqual(self.value("front_camera"), "24 MP")
        self.assertEqual(self.value("front_camera_aperture"), "ƒ/2.0")
        self.assertNotIn("front_camera", self.value("rear_camera"))

    def test_video_subsections_are_bound_to_their_camera(self):
        self.assertEqual(self.value("rear_video_recording"), "8K at 30 FPS; 4K at 24/30/60 FPS")
        self.assertEqual(self.value("front_video_recording"), "4K at 30/60 FPS")
        self.assertEqual(self.value("slow_motion_video"), "240")
        self.assertEqual(self.value("hdr_video_recording"), "10-bit")
        self.assertEqual(self.value("video_formats"), "HEVC (H.265), AVC (H.264)")
        self.assertIn("Stereo recording", self.value("video_audio_features"))

    def test_inline_footnote_numbers_are_removed(self):
        self.assertIn("Dual exposure on wide camera", self.value("rear_video_features"))
        self.assertNotIn("camera12", self.value("rear_video_features"))

    def test_connectivity_parent(self):
        self.assertEqual(self.value("wifi"), "Wi-Fi 6E (802.11ax)")
        self.assertEqual(self.value("wifi_bands"), "2.4 GHz + 5 GHz + 6 GHz")
        self.assertEqual(self.value("wifi_mimo"), "2x2")
        self.assertEqual(self.value("bluetooth"), "Bluetooth 5.2")
        self.assertEqual(self.value("nfc"), "Yes")
        self.assertEqual(self.value("gnss"), "GPS; GLONASS; Galileo")
        self.assertEqual(self.value("usb"), "USB Type-C 3.1")
        self.assertEqual(self.value("operating_system"), "Zeta OS 3")

    def test_network_is_summarized_and_details_are_separate_attributes(self):
        self.assertEqual(self.value("network_generations"), "2G; 3G; 4G; LTE; 5G")
        self.assertEqual(self.value("network_5g_type"), "mmWave + Sub-6 GHz")
        self.assertEqual(self.value("model_number"), "AX1")
        self.assertEqual(self.value("gsm_bands"), "850, 900, 1800, 1900 MHz")
        self.assertTrue(self.value("lte_bands").startswith("B1, B2, B3"))
        self.assertTrue(self.value("lte_bands").endswith("B48, B66, B71"))     # wrapped value rejoined
        self.assertTrue(self.value("nr_sub6_bands").endswith("n77, n78"))
        self.assertEqual(self.value("nr_mmwave_bands"), "n258, n260, n261")
        # the summary never carries band lists
        self.assertNotIn("B1", self.value("network_generations"))
        self.assertEqual(self.value("esim"), "Yes")

    def test_lists_units_and_misc(self):
        self.assertEqual(self.value("software_update_support"), "5")
        self.assertEqual(self.value("ip_rating"), "IP68")
        self.assertEqual(self.value("color"), "Graphite; Sand; Mint")
        self.assertEqual(self.value("package_contents"), "1 m USB-C cable; SIM tool")
        self.assertEqual(self.value("microphone_count"), "2")
        self.assertEqual(self.value("hearing_aid_compatible"), "Yes")
        self.assertEqual(self.value("conversational_gain"), "14 dB w/ aid & 18 dB w/o aid (DA 23-914)")
        self.assertEqual(self.value("haptic_engine"), "Linear resonant")     # unknown-but-valid label

    def test_dimensions_are_composed_from_metric_components(self):
        heights = [a for a in self.attrs if a.name == "Product height"]
        self.assertEqual((heights[0].value, heights[0].unit), ("150.0", "mm"))


class ModelColumnIsolationTests(unittest.TestCase):
    def test_max_column_never_leaks_into_base_model_and_vice_versa(self):
        _, _, base = atomic("Acme Phone 5")
        _, diagnostics, maxi = atomic("Acme Phone 5 Max")
        self.assertEqual(diagnostics["selected_column"], "Acme Phone 5 Max")
        for canonical, base_value, max_value in [
            ("display_size", "6.1", "6.7"), ("battery_capacity", "4400", "5000"),
            ("peak_brightness", "2600", "2900"), ("ram", "8", "12"), ("model_number", "AX1", "AX2"),
            ("net_weight", "187", "221"), ("battery_life", "30+", "40+"),
        ]:
            self.assertEqual(base[canonical]["value"], base_value, canonical)
            self.assertEqual(maxi[canonical]["value"], max_value, canonical)
        self.assertTrue(base["lte_bands"]["value"].endswith("B71"))
        self.assertFalse(maxi["lte_bands"]["value"].endswith("B71"))

    def test_no_exact_column_means_no_data(self):
        attrs, diagnostics, facts = atomic("Acme Phone 5 Mini")
        self.assertEqual((attrs, facts), ([], {}))
        self.assertEqual(diagnostics["outcome"], "no_exact_model_column")

    def test_family_title_does_not_break_exact_match(self):
        from core.match import candidate_model_match
        self.assertEqual(
            candidate_model_match("Acme Phone 5", "The Acme Phone 5 and the Acme Phone 5 Max", SPECS_URL),
            "exact",
        )


class AliasNormalizationTests(unittest.TestCase):
    def test_synonyms_share_one_canonical_attribute(self):
        for label in ("Peak brightness", "Maximum brightness", "Max brightness", "peak display brightness"):
            self.assertEqual(resolve_canonical(label), "peak_brightness", label)
        self.assertEqual(resolve_canonical("Screen glass"), "cover_glass")
        self.assertEqual(resolve_canonical("Ultra wideband"), "uwb")

    def test_unknown_synonym_line_lands_on_the_existing_canonical_not_a_duplicate(self):
        lines = [Line("Maximum brightness: 2500 nits", False, 0)]
        facts, ledger = read_section("Brightness", lines)
        self.assertEqual([f.canonical for f in facts], ["peak_brightness"])
        self.assertEqual(ledger[0]["outcome"], "accepted")

    def test_unknown_labels_get_a_stable_canonical(self):
        self.assertEqual(resolve_canonical("Haptic engine"), "haptic_engine")


class NoSilentDropTests(unittest.TestCase):
    def test_every_line_has_an_explicit_outcome_and_rejections_have_reasons(self):
        _, diagnostics, _ = atomic()
        allowed = {"accepted", "merged", "heading", "rejected"}
        self.assertTrue(diagnostics["ledger"])
        for entry in diagnostics["ledger"]:
            self.assertIn(entry["outcome"], allowed)
            if entry["outcome"] in {"rejected", "merged"}:
                self.assertTrue(entry["reason"], entry)
            if entry["outcome"] == "accepted":
                self.assertTrue(entry["attributes"], entry)

    def test_known_rejections_are_named(self):
        _, diagnostics, _ = atomic()
        by_line = {e["line"]: e for e in diagnostics["ledger"]}
        self.assertEqual(by_line["acme.example/security"]["outcome"], "rejected")
        self.assertIn("link", by_line["acme.example/security"]["reason"])
        self.assertIn("documentation", by_line["Regulatory & safety documentation"]["reason"])
        self.assertEqual(by_line["6.6 oz"]["outcome"], "merged")
        self.assertEqual(by_line["(LTPO)"]["outcome"], "merged")

    def test_every_atomic_fact_reaches_the_profile_or_has_a_reason(self):
        result = run_page({SPECS_URL: specs_html()}, [official_candidate(SPECS_URL)])
        diagnostics = result.final_profile.metadata["official_spec_table"][0]
        diff = completeness_diff(diagnostics["facts"], result.final_profile.by_name)
        self.assertEqual(len(diff), len(diagnostics["facts"]))
        for entry in diff:
            self.assertIn(entry["outcome"], {"accepted", "rejected"})
            if entry["outcome"] == "rejected":
                self.assertTrue(entry["reason"], entry)
        accepted = [e for e in diff if e["outcome"] == "accepted"]
        self.assertGreaterEqual(len(accepted), 0.95 * len(diff), [e for e in diff if e["outcome"] != "accepted"])

    def test_bold_sentence_is_content_not_a_heading(self):
        self.assertTrue(is_heading(Line("Video Features", True, 0)))
        self.assertFalse(is_heading(Line("Triple rear camera system: 48 MP wide", True, 0)))
        self.assertFalse(is_heading(Line("Audio", False, 0)))

    def test_wrapped_values_are_rejoined_and_bands_expanded(self):
        merged = merge_continuations([Line("LTE: Bands B1/2", False, 0), Line("/66/71", False, 1),
                                      Line("5G Sub-6", False, 2), Line(": Bands n1/2", False, 3)])
        self.assertEqual([m.text for m in merged], ["LTE: Bands B1/2/66/71", "5G Sub-6 : Bands n1/2"])
        self.assertEqual(expand_bands("B1/2/66"), "B1, B2, B66")
        self.assertEqual(expand_bands("1,2,4"), "1, 2, 4")


class DynamicSchemaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = run_page(
            {SPECS_URL: specs_html(), SECONDARY_URL: GSM_HTML},
            [official_candidate(SPECS_URL), secondary_candidate()],
        )
        cls.facts = cls.result.final_profile.by_name
        cls.service = _to_result(VerifyProductRequest("Acme", "Phone 5"), cls.result)

    def test_schema_is_extended_beyond_the_static_smartphone_schema(self):
        static = {d.canonical_name for d in get_attribute_schema("smartphone")}
        dynamic = [n for n, a in self.facts.items() if n not in static and not a.discovered]
        self.assertGreater(len(dynamic), 30)
        for name in ("peak_brightness", "lte_bands", "network_generations", "haptic_engine", "wireless_charging"):
            self.assertIn(name, dynamic)
            self.assertEqual(self.facts[name].status, "Confirmed")
            self.assertFalse(self.facts[name].discovered)

    def test_dynamic_attributes_are_confirmed_from_the_official_source(self):
        for name in ("peak_brightness", "fast_charging", "wide_camera_aperture", "gsm_bands"):
            self.assertEqual(self.facts[name].authority_status, "verified")
            self.assertEqual(self.facts[name].supporting_sources[0].source_type, "manufacturer")

    def test_dynamic_attributes_are_user_facing_in_preview_and_csv(self):
        text = "\n".join(format_result(self.service, language="en"))
        self.assertIn("Peak Brightness = 2600 nits", text)
        row = build_wide_export_row(self.service, language="en")
        self.assertEqual(row["Peak Brightness"], "2600 nits")
        self.assertEqual(row["LTE Bands"].split(", ")[0], "B1")

    def test_secondary_fills_only_what_the_official_page_lacks(self):
        self.assertEqual(self.facts["gpu"].source, SECONDARY_URL)
        for name in ("display_size", "battery_capacity", "processor"):
            self.assertEqual(self.facts[name].source, SPECS_URL)

    def test_static_schema_is_not_mutated_by_a_run(self):
        self.assertNotIn("peak_brightness", {d.canonical_name for d in get_attribute_schema("smartphone")})

    def test_variant_gate_still_rejects_unbound_variant_facts(self):
        page = ("<html><body><h1>Acme Phone 5</h1><table>"
                "<tr><th>Display size</th><td>6.1 in</td></tr><tr><th>Processor</th><td>Acme Silicon A2</td></tr>"
                "<tr><th>Battery capacity</th><td>4400 mAh</td></tr><tr><th>RAM</th><td>8 GB</td></tr>"
                "</table></body></html>")
        result = run_page({SPECS_URL: page}, [official_candidate(SPECS_URL)])
        self.assertNotEqual(result.final_profile.by_name["ram"].status, "Confirmed")


class LanguageParityTests(unittest.TestCase):
    """The pipeline is language-neutral; only presentation differs."""

    @classmethod
    def setUpClass(cls):
        cls.result = run_page(
            {SPECS_URL: specs_html(), SECONDARY_URL: GSM_HTML},
            [official_candidate(SPECS_URL), secondary_candidate()],
        )
        cls.service = _to_result(VerifyProductRequest("Acme", "Phone 5"), cls.result)
        cls.en = "\n".join(format_result(cls.service, language="en"))
        cls.ru = "\n".join(format_result(cls.service, language="ru"))

    def test_core_and_services_do_not_know_about_language(self):
        root = Path(__file__).resolve().parent.parent
        for folder in ("core", "services"):
            for path in (root / folder).glob("*.py"):
                text = path.read_text(encoding="utf-8")
                self.assertNotRegex(text, r"(?m)^\s*(?:from|import) bot\b|\blanguage\s*[:=,)]|\"language\"", str(path))

    def test_found_count_and_denominator_are_identical(self):
        found, total = found_counts(self.service)
        self.assertIn(f"Found: {found}/{total}", self.en)
        self.assertIn(f"Найдено: {found}/{total}", self.ru)
        self.assertGreater(total, 60)

    def test_same_number_of_preview_attribute_lines_and_same_order(self):
        def names(text):
            return [line.split(" = ")[0] for line in text.split("\n") if " = " in line]
        en, ru = names(self.en), names(self.ru)
        self.assertEqual(len(en), len(ru))
        self.assertEqual(len(en), len(set(en)))
        self.assertEqual(len(ru), len(set(ru)))
        self.assertEqual(len(en), found_counts(self.service)[0])
        # same canonical attributes, same order: compare through canonical names
        confirmed = [a.canonical_name for a in self.service.attributes
                     if a.status == "Confirmed" and not a.discovered
                     and a.canonical_name not in {"brand", "model", "manufacturer_article"}]
        by_priority = sorted(
            (a for a in self.service.attributes if a.canonical_name in confirmed),
            key=lambda a: ({"critical": 0, "high": 1, "medium": 2, "low": 3}.get(a.priority, 3), a.canonical_name),
        )
        self.assertEqual(
            [display_name(a.canonical_name, "en", fallback=a.display_name) for a in by_priority][:20], en[:20])
        self.assertEqual(
            [display_name(a.canonical_name, "ru", fallback=a.display_name) for a in by_priority][:20], ru[:20])

    def test_sources_are_identical(self):
        self.assertEqual(re.findall(r"https://\S+", self.en), re.findall(r"https://\S+", self.ru))
        card_en = format_auxiliary(self.service, language="en")
        card_ru = format_auxiliary(self.service, language="ru")
        self.assertEqual(card_en[1], card_ru[1])
        self.assertEqual(re.findall(r"https://\S+", card_en[0]), re.findall(r"https://\S+", card_ru[0]))
        self.assertEqual([r[0] for r in product_image_records(self.service)],
                         [r[0] for r in product_image_records(self.service)])

    def test_csv_columns_and_order_match_and_headers_are_localized(self):
        en = list(csv.reader(StringIO(export_result_csv(self.service, language="en"))))
        ru = list(csv.reader(StringIO(export_result_csv(self.service, language="ru"))))
        self.assertEqual((len(en), len(ru)), (2, 2))                       # header + one product row
        self.assertEqual(len(en[0]), len(ru[0]))
        self.assertEqual(len(en[1]), len(ru[1]))
        self.assertEqual(len(en[0]), len(set(en[0])))
        self.assertEqual(len(ru[0]), len(set(ru[0])))
        populated = lambda header, row, empty: sum(1 for h, c in zip(header, row) if c not in {empty})
        self.assertEqual(populated(en[0], en[1], "Not found"), populated(ru[0], ru[1], "Не найдено"))
        self.assertIn("Peak Brightness", en[0])
        self.assertIn("Пиковая яркость", ru[0])
        self.assertIn("Диапазоны LTE", ru[0])
        self.assertIn("Диафрагма широкоугольной камеры", ru[0])

    def test_units_yes_no_and_decimals_differ_only_in_presentation(self):
        self.assertIn("Display Size = 6.1 in", self.en)
        self.assertIn("Размер экрана = 6,1 дюйма", self.ru)
        self.assertIn("Peak Brightness = 2600 nits", self.en)
        self.assertIn("Пиковая яркость = 2600 нит", self.ru)
        self.assertIn("Wireless Charging = Yes", self.en)
        self.assertIn("Беспроводная зарядка = Да", self.ru)
        self.assertIn("Battery Life = 30+ h", self.en)
        self.assertIn("Время работы от аккумулятора = 30+ ч", self.ru)
        self.assertIn("Software Update Support = 5 years", self.en)
        self.assertIn("Срок поддержки обновлений = 5 лет", self.ru)
        for untranslated in ("Acme Silicon A2", "USB Type-C 3.1", "Wi-Fi 6E (802.11ax)", "Bluetooth 5.2", "IP68"):
            self.assertIn(untranslated, self.en)
            self.assertIn(untranslated, self.ru)

    def test_every_catalog_attribute_has_both_language_names(self):
        for canonical in catalog_canonicals():
            self.assertIn(canonical, ATTRIBUTE_DISPLAY_NAMES, canonical)
            self.assertNotEqual(display_name(canonical, "ru"), display_name(canonical, "en"), canonical) \
                if canonical not in {"nfc", "esim", "wifi_mimo"} else None

    def test_feature_lists_are_localized_item_by_item_without_damaging_names(self):
        from bot.localize import localize_value
        value = "Stereo recording; Super Res Zoom up to 30x; High-Res (up to 50MP)"
        self.assertEqual(
            localize_value("camera_features", value, None, "ru"),
            "Стереозапись; Super Res Zoom до 30×; Высокое разрешение (до 50 Мп)",
        )
        self.assertEqual(
            localize_value("camera_features", "Pixel UltraVision 50MP", None, "ru"),
            "Pixel UltraVision 50MP",
        )
        self.assertEqual(localize_value("speakers", "Stereo", None, "ru"), "Стерео")
        self.assertEqual(localize_value("rear_video_recording", "8K at 30 FPS; 4K at 24/30/60 FPS", None, "ru"),
                         "8K, 30 кадр/с; 4K, 24/30/60 кадр/с")

    def test_category_and_status_labels_are_localized(self):
        self.assertIn("Smartphone", self.en)
        self.assertIn("Смартфон", self.ru)


if __name__ == "__main__":
    unittest.main()
