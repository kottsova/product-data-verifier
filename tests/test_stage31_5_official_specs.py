"""Stage 31.5 regression: official spec-table coverage + rich secondary UX.

Deterministic, no network. The fixture reproduces the *structure* of a real
official side-by-side spec page (an accessible comparison table: ``th
scope=col`` model columns, ``th scope=colgroup`` sections, ``td headers=...``
cells) for "Pixel 9 Pro" and "Pixel 9 Pro XL". The extraction code itself
carries no Pixel/Google knowledge.
"""

from __future__ import annotations

import asyncio
import csv
from dataclasses import replace
from io import StringIO
import unittest

from bot.export import build_wide_export_row, export_result_csv
from bot.formatters import (
    format_auxiliary,
    format_result,
    found_counts,
    product_image_records,
    product_image_urls,
)
from bot.handlers import build_photos_callback, build_verify_command
from bot.jobs import JobManager
from bot.localize import localize_value
from bot.attribute_filter import filter_user_facing
from core.discovery import rank_candidates
from core.identity import resolve_product_identity
from core.official_source import (
    extract_official_spec_links,
    select_product_image_records,
)
from core.official_spec_table import (
    extract_official_section_specs,
    section_roles,
    select_model_column,
)
from core.workflow import ProductWorkflowRequest, WorkflowServices, run_product_workflow
from services.product_verifier import VerifyProductRequest, _to_result
from tests.test_bot import (
    FakeCallbackQuery,
    FakeCallbackUpdate,
    FakeMessage,
    FakeUpdate,
    TrackingFakeService,
    _tap_language_button,
    make_result,
)
from tests.test_workflow import candidate, fetch_result

SPECS_URL = "https://fi.google.example/about/phones/pixel-9-pro-specs"
PRODUCT_URL = "https://fi.google.example/about/phones/pixel-9-pro"
SECONDARY_URL = "https://www.gsm.example/google_pixel_9_pro-13218.php"

# section heading -> (Pro column lines, Pro XL column lines)
SECTIONS = [
    ("Display", (
        ["6.3-inch Super Actua display", "(LTPO)", "1280 x 2856 LTPO OLED at 495 PPI",
         "20:9 aspect ratio", "Smooth Display (1-120Hz)"],
        ["6.8-inch Super Actua display", "(LTPO)", "1344 x 2992 LTPO OLED at 486 PPI",
         "20:9 aspect ratio", "Smooth Display (1-120Hz)"],
    )),
    ("Dimensions and Weight", (
        ["6.0 height x 2.8 width x 0.3 depth (inches)",
         "152.8 height x 72.0 width x 8.5 depth (mm)", "7.0 oz", "199 g"],
        ["6.4 height x 3.0 width x 0.3 depth (inches)",
         "162.8 height x 76.6 width x 8.5 depth (mm)", "7.8 oz", "221 g"],
    )),
    ("Battery and Charging", (
        ["Typical 4700 mAh (Minimum 4558 mAh)", "Fast charging - up to 55% in about 30 minutes",
         "- using Google 45W USB-C Charger, sold separately", "Fast wireless charging (Qi-certified)"],
        ["Typical 5060 mAh (Minimum 4942 mAh)", "Fast charging - up to 70% in about 30 minutes",
         "- using Google 45W USB-C Charger, sold separately", "Fast wireless charging (Qi-certified)"],
    )),
    ("Memory and Storage", (
        ["16 GB RAM", "128 GB / 256 GB / 512 GB / 1 TB"],
        ["16 GB RAM", "128 GB / 256 GB / 512 GB / 1 TB"],
    )),
    ("Processors", (
        ["Google Tensor G4", "Titan M2 security coprocessor"],
        ["Google Tensor G4", "Titan M2 security coprocessor"],
    )),
    ("Rear Camera", (
        ["Pro triple rear camera system: 50 MP wide | 48 MP ultrawide with Macro Focus | "
         "48 MP 5x telephoto lens", "50 MP Octa PD wide camera", "1/1.31\" image sensor size"],
        ["Pro triple rear camera system: 50 MP wide | 48 MP ultrawide with Macro Focus | "
         "48 MP 5x telephoto lens", "50 MP Octa PD wide camera"],
    )),
    ("Front Camera", (
        ["42 MP Dual PD selfie camera with autofocus"],
        ["42 MP Dual PD selfie camera with autofocus"],
    )),
    ("Video", (
        ["Rear Camera", "8K video recording at 30 FPS", "Front Camera", "4K video recording"],
        ["Rear Camera", "8K video recording at 30 FPS", "Front Camera", "4K video recording"],
    )),
    ("Materials and durability", (
        ["Scratch-resistant cover glass", "IP68 dust and water resistance"],
        ["Scratch-resistant cover glass", "IP68 dust and water resistance"],
    )),
    ("Security and OS updates", (["7 years of OS, security, and Pixel Drop updates"],) * 2),
    ("OS", (["Launched with Android 14"], ["Launched with Android 14"])),
    ("Ports", (["USB Type-C® 3.2", "Power button"], ["USB Type-C® 3.2", "Power button"])),
    ("Wireless & Location", (
        ["Wi-Fi 7 (802.11be) with 2.4GHz+5GHz+6GHz", "Bluetooth® v5.3 with dual antennas",
         "NFC", "Dual SIM (Single Nano SIM and eSIM)"],
        ["Wi-Fi 7 (802.11be) with 2.4GHz+5GHz+6GHz", "Bluetooth® v5.3 with dual antennas",
         "NFC", "Dual SIM (Single Nano SIM and eSIM)"],
    )),
    ("5G", (["5G mmWave + Sub 6GHz", "Model GR83Y"], ["5G mmWave + Sub 6GHz", "Model GGX8B"])),
]


def specs_html(columns=("Pixel 9 Pro", "Pixel 9 Pro XL"), sections=SECTIONS) -> str:
    rows = ['<tr>' + "".join(
        f'<th scope="col" id="c{i}">{name}</th>' for i, name in enumerate(columns)
    ) + '</tr>']
    for index, (heading, cells) in enumerate(sections):
        rows.append(
            f'<tr><th scope="colgroup" id="s{index}">{heading}</th>'
            f'<th scope="colgroup">{heading}</th></tr>'
        )
        rows.append("<tr>" + "".join(
            f'<td headers="c{i} s{index}">'
            + "".join(f"<div>{line}</div>" for line in cells[i if i < len(cells) else 0])
            + "</td>"
            for i in range(len(columns))
        ) + "</tr>")
    return (
        "<html><head><title>Pixel 9 Pro | Tech Specs</title></head><body>"
        "<h1>Pixel 9 Pro</h1><table><tbody>" + "".join(rows) + "</tbody></table>"
        '<img src="https://lh3.googleusercontent.com/aaa=w1200" alt="Pixel 9 Pro front">'
        '<img src="https://lh3.googleusercontent.com/bbb=w1200" alt="Pixel 9 Pro back">'
        "</body></html>"
    )


PRODUCT_HTML = (
    "<html><head><title>Pixel 9 Pro | Overview</title></head><body><h1>Pixel 9 Pro</h1>"
    '<a href="/about/phones/pixel-9-pro?hl=en-US">Overview</a>'
    '<a href="/about/phones/pixel-9-pro-specs?hl=en-US">Tech Specs</a>'
    '<a href="/about/phones/pixel-9-pro-xl-specs">Tech Specs</a>'
    '<a href="https://elsewhere.example/pixel-9-pro-specs">Tech Specs</a>'
    "</body></html>"
)

GSM_HTML = (
    '<html><head><meta property="og:image" content="https://fdn.gsm.example/bigpic/pixel.jpg"></head><body>'
    '<a href="google_pixel_9_pro-review-2745.php">Review</a>'
    '<a href="google_pixel_9_pro-pictures-13218.php">Pictures</a>'
    '<a href="google_pixel_9_pro-reviews-13218.php">Opinions</a>'
    '<a href="compare.php3?idPhone1=13218">Compare</a>'
    '<a href="google_pixel_9_pro-price-13218.php">Prices</a>'
    '<a href="related.php3?idPhone=13218">Related devices</a>'
    '<a href="videos.php3">Videos</a><a href="reviews.php3">Reviews</a>'
    "<table><tr><th>GPU</th><td>Mali-G715 MC7</td></tr>"
    "<tr><th>Chipset</th><td>Google Tensor G4</td></tr></table>"
    "</body></html>"
)


def official_candidate(url, **kwargs):
    item = candidate(url, title="Pixel 9 Pro", relation="same_base_model", **kwargs)
    item["authority_evidence_url"] = "https://fi.google.example/"
    return item


def secondary_candidate():
    item = candidate(
        SECONDARY_URL, title="Google Pixel 9 Pro", source_type="specialized_reference",
        authority="unknown", relation="same_base_model", score=80,
    )
    item["authority_evidence_url"] = SECONDARY_URL
    return item


def run_pixel(pages, initial, model="Pixel 9 Pro"):
    from core.discovery import DiscoveryOutcome
    from core.extract import extract_attributes

    def fetch(item):
        page = pages[item["url"]]
        source = fetch_result(item)
        source["html"] = page
        source["text"] = ""
        return source

    outcome = DiscoveryOutcome(list(initial), "success", ["initial"], ["initial"], [])
    services = WorkflowServices(
        lambda identity, market: outcome,
        lambda identity, query, market: DiscoveryOutcome([], "success", [query], [query], []),
        fetch,
        extract_attributes,
    )
    return run_product_workflow(
        ProductWorkflowRequest(
            f"Google {model}", brand="Google", targeted_search_enabled=False,
        ),
        services=services,
    )


def identity_for(model: str):
    return resolve_product_identity(model, "Google")


def spec_source(html=None, **overrides):
    source = {
        "status": "success", "html": html or specs_html(),
        "final_url": SPECS_URL, "source_url": SPECS_URL, "source_type": "manufacturer",
        "authority_status": "verified", "model_relevance": "exact_base_model",
        "identity_relation": "same_base_model", "document_type": "html",
    }
    source.update(overrides)
    return source


class SpecPageAcceptanceTests(unittest.TestCase):
    """1. `/pixel-9-pro-specs` is an exact official page, not dropped."""

    def test_specs_page_is_ranked_as_verified_exact_official(self):
        ranked = rank_candidates(
            [(SPECS_URL, "Get the new Pixel 9 Pro and the Pixel 9 Pro XL | Google Fi")],
            "Google", "Pixel 9 Pro",
            official_domains={"google.example": "https://fi.google.example/"},
        )
        self.assertEqual(len(ranked), 1)
        self.assertEqual(ranked[0]["url"], SPECS_URL)
        self.assertEqual(ranked[0]["source_type"], "manufacturer")
        self.assertEqual(ranked[0]["authority_status"], "verified")
        self.assertEqual(ranked[0]["model_match"], "exact")

    def test_specs_page_is_reached_from_the_product_page_tech_specs_link(self):
        links = extract_official_spec_links(
            spec_source(PRODUCT_HTML, final_url=PRODUCT_URL, source_url=PRODUCT_URL),
            identity_for("Pixel 9 Pro"),
        )
        # the model's own specs link only: not the XL sibling, not another host
        self.assertEqual(links, ["https://fi.google.example/about/phones/pixel-9-pro-specs?hl=en-US"])

    def test_workflow_follows_the_link_when_search_only_found_the_product_page(self):
        result = run_pixel(
            {PRODUCT_URL: PRODUCT_HTML,
             "https://fi.google.example/about/phones/pixel-9-pro-specs?hl=en-US": specs_html()},
            [official_candidate(PRODUCT_URL)],
        )
        facts = result.final_profile.by_name
        self.assertEqual(facts["battery_capacity"].value, "4700")
        fetched = {item["source_url"] for item in result.fetched_sources}
        self.assertIn("https://fi.google.example/about/phones/pixel-9-pro-specs?hl=en-US", fetched)

    def test_sibling_model_specs_page_is_never_followed_or_fetched(self):
        result = run_pixel(
            {PRODUCT_URL: PRODUCT_HTML,
             "https://fi.google.example/about/phones/pixel-9-pro-specs?hl=en-US": specs_html()},
            [official_candidate(PRODUCT_URL)],
        )
        fetched = {item["source_url"] for item in result.fetched_sources}
        self.assertFalse(any("xl-specs" in url or "elsewhere" in url for url in fetched))


class ModelColumnBindingTests(unittest.TestCase):
    """2. Pro / Pro XL columns are never merged."""

    def facts(self, model):
        attrs, diagnostics = extract_official_section_specs(spec_source(), identity_for(model))
        return {a.name: a for a in attrs}, diagnostics

    def test_pro_column_is_selected_for_pro(self):
        facts, diagnostics = self.facts("Pixel 9 Pro")
        self.assertEqual(diagnostics["selected_column"], "Pixel 9 Pro")
        self.assertEqual(facts["Display size"].value, "6.3")
        self.assertEqual(facts["Display resolution"].value, "1280 x 2856")
        self.assertEqual(facts["Battery capacity"].value, "4700")
        self.assertEqual(facts["Net weight"].value, "199")

    def test_xl_column_is_selected_for_xl_and_never_leaks_into_pro(self):
        facts, diagnostics = self.facts("Pixel 9 Pro XL")
        self.assertEqual(diagnostics["selected_column"], "Pixel 9 Pro XL")
        self.assertEqual(facts["Display size"].value, "6.8")
        self.assertEqual(facts["Battery capacity"].value, "5060")
        self.assertEqual(facts["Net weight"].value, "221")

    def test_model_absent_from_the_columns_yields_nothing(self):
        facts, diagnostics = self.facts("Pixel 9")
        self.assertEqual(facts, {})
        self.assertEqual(diagnostics["outcome"], "no_exact_model_column")

    def test_sibling_suffix_is_not_an_exact_match(self):
        self.assertEqual(
            select_model_column(["Pixel 9 Pro XL", "Pixel 9 Pro"], identity_for("Pixel 9 Pro")), 1,
        )
        self.assertIsNone(select_model_column(["Pixel 9 Pro XL"], identity_for("Pixel 9 Pro")))

    def test_unlabeled_columns_only_share_values_they_agree_on(self):
        html = specs_html(columns=("Column A", "Column B"))
        attrs, diagnostics = extract_official_section_specs(
            spec_source(html), identity_for("Pixel 9 Pro"),
        )
        names = {a.name: a.value for a in attrs}
        self.assertEqual(diagnostics["selected_column"], "shared")
        self.assertEqual(names["RAM"], "16")            # identical in both columns
        self.assertNotIn("Display size", names)         # 6.3 vs 6.8
        self.assertNotIn("Battery capacity", names)     # 4700 vs 5060
        self.assertNotIn("Net weight", names)

    def test_section_meaning_not_selectors(self):
        self.assertIn("battery", section_roles("Battery and Charging"))
        self.assertIn("memory", section_roles("Memory & Storage"))
        self.assertIn("rear_camera", section_roles("Main Camera"))
        self.assertIn("front_camera", section_roles("Selfie camera"))
        self.assertNotIn("os", section_roles("Security and OS updates"))


class ExtractedFieldTests(unittest.TestCase):
    """3-12. Canonical fields reach the final profile."""

    @classmethod
    def setUpClass(cls):
        cls.result = run_pixel({SPECS_URL: specs_html()}, [official_candidate(SPECS_URL)])
        cls.facts = cls.result.final_profile.by_name

    def confirmed(self, name):
        fact = self.facts[name]
        self.assertEqual(fact.status, "Confirmed", (name, fact.resolution_reason))
        self.assertEqual(fact.authority_status, "verified")
        return fact

    def test_dimensions_extracted(self):
        value = self.confirmed("product_dimensions").value
        self.assertEqual((value.height, value.width, value.depth, value.unit),
                         ("152.8", "72.0", "8.5", "mm"))

    def test_weight_extracted(self):
        self.assertEqual(self.confirmed("net_weight").value, "199")

    def test_battery_4700_mah_extracted(self):
        fact = self.confirmed("battery_capacity")
        self.assertEqual((fact.value, fact.unit), ("4700", "mAh"))

    def test_ram_16_gb_extracted_despite_variant_level_scope(self):
        fact = self.confirmed("ram")
        self.assertEqual((fact.value, fact.unit), ("16", "GB"))

    def test_storage_extracted(self):
        self.assertIn("1 TB", str(self.confirmed("storage").value))

    def test_ip68_extracted(self):
        self.assertEqual(self.confirmed("ip_rating").value, "IP68")

    def test_usb_type_c_3_2_extracted(self):
        self.assertEqual(self.confirmed("usb").value, "USB Type-C 3.2")

    def test_wifi_7_extracted(self):
        self.assertIn("Wi-Fi 7", str(self.confirmed("wifi").value))

    def test_dual_sim_extracted(self):
        self.assertIn("Dual SIM", str(self.confirmed("sim").value))

    def test_android_14_extracted(self):
        self.assertEqual(self.confirmed("operating_system").value, "Android 14")

    def test_remaining_canonical_fields(self):
        self.assertEqual(self.confirmed("display_size").value, "6.3")
        self.assertEqual(self.confirmed("display_type").value, "LTPO OLED")
        self.assertEqual(self.confirmed("refresh_rate").value, "1-120")
        self.assertEqual(self.confirmed("processor").value, "Google Tensor G4")
        self.assertEqual(self.confirmed("charging_power").value, "45")
        self.assertEqual(self.confirmed("bluetooth").value, "Bluetooth 5.3")
        self.assertEqual(self.confirmed("front_camera").value, "42 MP")

    def test_rear_camera_is_the_camera_not_the_video_section(self):
        value = str(self.confirmed("rear_camera").value)
        self.assertIn("50 MP wide", value)
        self.assertNotIn("8K", value)

    def test_xl_values_never_reach_the_pro_profile(self):
        for name in ("display_size", "battery_capacity", "net_weight", "display_resolution"):
            self.assertNotIn(str(self.facts[name].value), {"6.8", "5060", "221", "1344 x 2992"})

    def test_variant_gate_still_rejects_a_family_page_without_column_binding(self):
        # ram is variant-level: a base-model page's *unbound* fact must not be
        # promoted -- only column-bound (model_wide) facts are.
        page = (
            "<html><body><h1>Pixel 9 Pro</h1><table>"
            "<tr><th>Display size</th><td>6.3 in</td></tr>"
            "<tr><th>Processor</th><td>Google Tensor G4</td></tr>"
            "<tr><th>Battery capacity</th><td>4700 mAh</td></tr>"
            "<tr><th>RAM</th><td>16 GB</td></tr></table></body></html>"
        )
        result = run_pixel({SPECS_URL: page}, [official_candidate(SPECS_URL)])
        facts = result.final_profile.by_name
        self.assertEqual(facts["battery_capacity"].status, "Confirmed")
        self.assertNotEqual(facts["ram"].status, "Confirmed")


class LocalizationTests(unittest.TestCase):
    """4. RU presentation."""

    def test_units_and_values(self):
        cases = [
            ("display_size", "6.3 in", "6,3 дюйма"),
            ("display_size", "6 in", "6 дюймов"),
            ("display_resolution", "1280 x 2856 pixels", "1280 × 2856 пикселей"),
            ("ram", "16 GB", "16 ГБ"),
            ("storage", "128 GB / 256 GB / 512 GB / 1 TB", "128 ГБ / 256 ГБ / 512 ГБ / 1 ТБ"),
            ("battery_capacity", "4700 mAh", "4700 мА·ч"),
            ("charging_power", "45 W", "45 Вт"),
            ("refresh_rate", "120 Hz", "120 Гц"),
            ("net_weight", "199 g", "199 г"),
            ("product_dimensions", "152.8 × 72.0 × 8.5 mm", "152,8 × 72,0 × 8,5 мм"),
            ("front_camera", "42 MP", "42 Мп"),
            ("timer", "Yes", "Да"),
            ("timer", "No", "Нет"),
        ]
        for name, source, expected in cases:
            self.assertEqual(localize_value(name, source, None, "ru"), expected, name)

    def test_camera_roles(self):
        self.assertEqual(
            localize_value("rear_camera", "50 MP wide + 48 MP telephoto", None, "ru"),
            "50 Мп широкоугольная + 48 Мп телефото",
        )

    def test_names_and_standards_are_never_translated(self):
        for name, value in [
            ("processor", "Google Tensor G4"),
            ("gpu", "Mali-G715 MC7"),
            ("usb", "USB Type-C 3.2"),
            ("wifi", "Wi-Fi 7 (802.11be)"),
            ("bluetooth", "Bluetooth 5.3"),
            ("ip_rating", "IP68"),
            ("operating_system", "Android 14"),
            ("model", "GR83Y"),
        ]:
            self.assertEqual(localize_value(name, value, None, "ru"), value, name)

    def test_english_is_untouched(self):
        self.assertEqual(localize_value("ram", "16 GB", None, "en"), "16 GB")
        self.assertEqual(localize_value("timer", "Yes", None, "en"), "Yes")

    def test_pixel_preview_and_csv_are_russian(self):
        result = pixel_service_result()
        text = "\n".join(format_result(result, language="ru"))
        self.assertIn("Смартфон", text)
        self.assertIn("Размер экрана = 6,3 дюйма", text)
        self.assertIn("Разрешение экрана = 1280 × 2856 пикселей", text)
        self.assertIn("Оперативная память = 16 ГБ", text)
        self.assertIn("Google Tensor G4", text)
        self.assertIn("USB Type-C 3.2", text)
        self.assertIn("Bluetooth 5.3", text)
        row = build_wide_export_row(result, language="ru")
        self.assertEqual(row["Категория"], "Смартфон")
        self.assertEqual(row["Оперативная память"], "16 ГБ")
        self.assertEqual(row["Размер экрана"], "6,3 дюйма")


def pixel_service_result(images=None):
    result = run_pixel(
        {SPECS_URL: specs_html(), SECONDARY_URL: GSM_HTML},
        [official_candidate(SPECS_URL), secondary_candidate()],
    )
    service_result = _to_result(VerifyProductRequest("Google", "Pixel 9 Pro"), result)
    return service_result


class PreviewFormatTests(unittest.TestCase):
    """7. Concise marketplace preview, 17. no duplicate/raw junk."""

    @classmethod
    def setUpClass(cls):
        cls.result = pixel_service_result()
        cls.text = "\n".join(format_result(cls.result, language="ru"))

    def test_header_then_sources_then_only_found_attributes(self):
        lines = self.text.split("\n")
        self.assertTrue(lines[0].startswith("📦 Google Pixel 9 Pro"))
        self.assertEqual(lines[1], "Смартфон")
        self.assertTrue(lines[2].startswith("Найдено: "))
        self.assertTrue(lines[3].startswith("🔗 Источники"))
        self.assertIn("официальный источник: " + SPECS_URL, self.text)
        self.assertLess(self.text.index(SPECS_URL), self.text.index(SECONDARY_URL))
        self.assertNotIn("Не определено", self.text)
        self.assertNotIn("▫️", self.text)
        for line in lines[lines.index("") + 1:]:
            if line and not line.startswith("⚠️"):
                self.assertIn(" = ", line)

    def test_found_count_equals_populated_export_columns(self):
        found, total = found_counts(self.result)
        row = build_wide_export_row(self.result, language="ru")
        canonical_columns = [
            key for key in row
            if key not in {"Бренд", "Модель", "Артикул", "Категория",
                           "Официальные источники", "Прочие источники"}
        ]
        populated = [key for key in canonical_columns if row[key] != "Не найдено"]
        self.assertEqual(found, len(populated))
        self.assertEqual(total, len(canonical_columns))
        self.assertIn(f"Найдено: {found}/{total}", self.text)

    def test_wide_csv_is_one_row_with_ru_headers(self):
        rows = list(csv.DictReader(StringIO(export_result_csv(self.result, language="ru"))))
        self.assertEqual(len(rows), 1)
        self.assertIn("Ёмкость аккумулятора", rows[0])
        self.assertEqual(rows[0]["Ёмкость аккумулятора"], "4700 мА·ч")

    def test_no_duplicate_labels_and_no_raw_junk(self):
        names = [line.split(" = ")[0] for line in self.text.split("\n") if " = " in line]
        self.assertEqual(len(names), len(set(names)))
        user_facing = filter_user_facing(self.result.attributes)
        self.assertEqual(len({a.canonical_name for a in user_facing}), len(user_facing))
        # (Stage 32: these fixture facts are legitimate atomic attributes now;
        # what must never appear is the sibling model's column or misaligned reads.)
        for junk in ("Pixel 9 Pro XL", "1344", "5060", "221", "Battery Share = ", "Rear Camera = 8K"):
            self.assertNotIn(junk, self.text)

    def test_conflicts_are_one_compact_warning_at_the_bottom(self):
        result = replace(self.result, conflicts=("gpu",))
        last = format_result(result, language="ru")[-1].rstrip().split("\n")[-1]
        self.assertEqual(sum(line.startswith("⚠️") for line in format_result(result, language="ru")[-1].split("\n")), 1)
        self.assertTrue(last.startswith("⚠️") or "Видеоядро" not in last)


class SecondaryUxTests(unittest.IsolatedAsyncioTestCase):
    """5. Rich secondary preview + auxiliary links, 16. no Related devices."""

    @classmethod
    def setUpClass(cls):
        cls.result = pixel_service_result()

    def test_auxiliary_card_keeps_review_opinions_compare_pictures_prices(self):
        text, preview_url = format_auxiliary(self.result, language="ru")
        self.assertEqual(preview_url, SECONDARY_URL)
        self.assertTrue(text.split("\n")[1] == SECONDARY_URL)
        for label in ("Обзор:", "Отзывы:", "Сравнение:", "Фотографии:", "Цены:"):
            self.assertIn(label, text)
        self.assertNotIn("Related", text)
        self.assertNotIn("related", text)
        # site-wide indexes are not product links
        self.assertNotIn("videos.php3", text)
        self.assertNotIn("reviews.php3", text)

    def test_english_labels(self):
        text, _ = format_auxiliary(self.result, language="en")
        for label in ("Review:", "Opinions:", "Compare:", "Pictures:", "Prices:"):
            self.assertIn(label, text)

    def test_no_related_devices_anywhere_in_the_result(self):
        joined = "\n".join(format_result(self.result, language="ru"))
        self.assertNotIn("related", joined.casefold())
        names = {a.canonical_name for a in self.result.attributes}
        self.assertFalse(any("related" in name for name in names))

    async def test_secondary_card_is_sent_below_with_its_own_rich_preview(self):
        manager = JobManager(TrackingFakeService(self.result), max_concurrent_jobs=1)
        message = FakeMessage()
        await _finish(manager, message)
        main = next(i for i, text in enumerate(message.sent) if "Найдено:" in text)
        card = next(i for i, text in enumerate(message.sent) if text.startswith("📎"))
        self.assertLess(main, card)                                  # below the result
        self.assertTrue(message.previews[main].is_disabled)          # official list: no preview
        self.assertFalse(message.previews[card].is_disabled)         # secondary: rich preview
        self.assertEqual(message.previews[card].url, SECONDARY_URL)
        self.assertLess(message.sent[main].index(SPECS_URL), message.sent[main].index(SECONDARY_URL))

    async def test_no_card_without_a_secondary_source(self):
        manager = JobManager(TrackingFakeService(make_result()), max_concurrent_jobs=1)
        message = FakeMessage()
        await _finish(manager, message)
        self.assertFalse(any(text.startswith("📎") for text in message.sent))


async def _finish(manager, message):
    verify = build_verify_command(manager)
    message.text = "Google Pixel 9 Pro"
    await verify(FakeUpdate(message), None)
    await _tap_language_button(manager, message, "ru")
    for _ in range(200):
        if manager.active_job_count() == 0 and len(message.sent) > 3:
            break
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.05)


def photo_buttons(message):
    return [
        (button.text, button.callback_data)
        for markup in message.reply_markups if markup is not None
        for row in markup.inline_keyboard for button in row
        if button.callback_data.startswith("photos:")
    ]


class PhotoTests(unittest.IsolatedAsyncioTestCase):
    """6. Photo button: official first, deduplicated, bounded, labeled."""

    def test_official_images_come_first_and_pipeline_records_them(self):
        result = run_pixel(
            {SPECS_URL: specs_html(), SECONDARY_URL: GSM_HTML},
            [official_candidate(SPECS_URL), secondary_candidate()],
        )
        records = result.final_profile.metadata["product_image_records"]
        self.assertTrue(records)
        self.assertEqual(records[0]["role"], "official")
        self.assertEqual(result.final_profile.metadata["product_images"][0], records[0]["url"])

    def test_secondary_tops_up_only_when_official_photos_are_few(self):
        official = spec_source()
        secondary = {**spec_source(GSM_HTML), "final_url": SECONDARY_URL, "source_url": SECONDARY_URL,
                     "source_type": "specialized_reference", "authority_status": "unknown"}
        records = select_product_image_records([secondary, official], "Pixel 9 Pro")
        self.assertEqual([r["role"] for r in records], ["official", "official", "secondary"])
        self.assertEqual(records[-1]["url"], "https://fdn.gsm.example/bigpic/pixel.jpg")

        many = "".join(
            f'<img src="https://cdn.google.example/i/{n}.jpg" alt="Pixel 9 Pro view {n}">'
            for n in range(6)
        )
        rich = spec_source(f"<html><body>{many}</body></html>")
        records = select_product_image_records([rich, secondary], "Pixel 9 Pro")
        self.assertEqual({r["role"] for r in records}, {"official"})

    def test_photos_are_bounded_deduplicated_and_free_of_logos(self):
        junk = (
            '<img src="https://cdn.google.example/logo.png" alt="Pixel 9 Pro logo">'
            '<img src="https://cdn.google.example/promo-banner.jpg" alt="Pixel 9 Pro">'
            '<img src="https://cdn.google.example/sprite.png" alt="Pixel 9 Pro">'
        )
        many = "".join(
            f'<img src="https://cdn.google.example/i/{n}.jpg" alt="Pixel 9 Pro view {n}">'
            for n in range(25)
        )
        duplicates = '<img src="https://cdn.google.example/i/0.jpg" alt="Pixel 9 Pro view 0">'
        source = spec_source(f"<html><body>{junk}{many}{duplicates}</body></html>")
        records = select_product_image_records([source], "Pixel 9 Pro")
        urls = [r["url"] for r in records]
        self.assertLessEqual(len(urls), 10)
        self.assertEqual(len(urls), len(set(urls)))
        self.assertFalse(any(word in u for u in urls for word in ("logo", "banner", "sprite")))

    async def test_button_shown_when_images_exist_and_labels_the_sources(self):
        with_images = replace(make_result(), metadata={"product_image_records": [
            {"url": f"https://cdn.google.example/{i}.jpg", "role": "official", "source": SPECS_URL}
            for i in range(2)
        ] + [{"url": "https://fdn.gsm.example/p.jpg", "role": "secondary", "source": SECONDARY_URL}]})
        manager = JobManager(TrackingFakeService(with_images), max_concurrent_jobs=1)
        message = PhotoMessage()
        await _finish(manager, message)
        buttons = photo_buttons(message)
        self.assertEqual([label for label, _ in buttons], ["📸 Скачать все фото"])
        prompt = next(text for text in message.sent if text.startswith("Найдено фото"))
        self.assertIn("официальных: 2", prompt)
        self.assertIn("из сторонних источников: 1", prompt)

        query = FakeCallbackQuery(buttons[0][1], message)
        await build_photos_callback(manager)(FakeCallbackUpdate(query), None)
        sent = [url for album in message.albums for url in album]
        self.assertEqual(sent[:2], [f"https://cdn.google.example/{i}.jpg" for i in range(2)])
        self.assertEqual(sent[-1], "https://fdn.gsm.example/p.jpg")
        self.assertTrue(message.captions[0].startswith("официальный источник"))
        self.assertTrue(message.captions[-1].startswith("сторонний источник"))

    async def test_no_button_without_images(self):
        manager = JobManager(TrackingFakeService(make_result()), max_concurrent_jobs=1)
        message = PhotoMessage()
        await _finish(manager, message)
        self.assertEqual(photo_buttons(message), [])

    def test_records_fall_back_to_legacy_official_only_metadata(self):
        legacy = replace(make_result(), metadata={"product_images": ["https://a/1.jpg", "http://a/2.jpg"]})
        self.assertEqual(product_image_urls(legacy), ["https://a/1.jpg"])
        self.assertEqual(product_image_records(legacy)[0][1], True)


class PhotoMessage(FakeMessage):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.albums: list[list[str]] = []
        self.captions: list[str] = []

    async def reply_media_group(self, media):
        self.albums.append([item.media for item in media])
        self.captions.extend(item.caption for item in media if item.caption)

    async def reply_photo(self, photo, caption=None):
        self.albums.append([photo])
        if caption:
            self.captions.append(caption)


if __name__ == "__main__":
    unittest.main()
