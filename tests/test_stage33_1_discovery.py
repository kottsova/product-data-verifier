"""Stage 33.1: stable official-page discovery, SKU structure, official documents."""

from __future__ import annotations

import time
import unittest
from unittest.mock import patch

import requests

from bot.discovery_formatters import format_discovery_result
from core.discovery import (
    DirectDomainProbeProvider,
    DiscoveryRuntimeConfig,
    ProviderAttempt,
    ResilientSearchSession,
    SearchResultRecord,
    _registrable_domain,
    _brand_root_domains,
    build_provider_query,
    clear_official_domain_cache,
    discover_with_status,
    strip_search_operators,
)
from core.official_documents import (
    OfficialDocument,
    canonical_document_url,
    classify_document,
    documents_by_canonical_field,
    extract_documents,
    merge_documents,
)
from core.page_inspection import fetch_working_page, inspect_product_page, url_variants
from core.sku import classify_sku_suffix, requested_sku, sku_relation, sku_search_terms
from services.discovery_debug import _result_from_outcome, document_queries


class FakeResponse:
    def __init__(self, url: str, text: str, status_code: int = 200):
        self.url = url
        self.text = text
        self.status_code = status_code
        self.headers = {"content-type": "text/html"}


class SkuStructureTests(unittest.TestCase):
    def setUp(self):
        clear_official_domain_cache()

    def test_slash_and_underscore_are_the_same_identifier(self):
        model = "Sonicare 9900 Prestige HX9992/12"
        for spelling in ("HX9992/12", "HX9992_12", "hx9992-12"):
            self.assertEqual(sku_relation(model, f"/c-p/{spelling}/toothbrush").kind, "exact", spelling)

    def test_requested_sku_splits_base_and_suffix(self):
        sku = requested_sku("Sonicare 9900 Prestige HX9992/12")
        self.assertEqual((sku.base, sku.suffix), ("HX9992", "12"))
        self.assertEqual(requested_sku("GAF-1825").base, "GAF1825")
        self.assertEqual(requested_sku("GAF-1825").suffix, "")
        self.assertIsNone(requested_sku("Pixel 9"))
        self.assertEqual(sku_search_terms("Sonicare 9900 Prestige HX9992/12")[0], "HX9992/12")

    def test_regional_suffix_is_not_a_different_model(self):
        relation = sku_relation("DCD796P2", "/en-gb/product/dcd796p2-gb/18v-xr-drill")
        self.assertEqual((relation.kind, relation.suffix), ("regional_suffix", "GB"))
        self.assertTrue(relation.same_model)
        self.assertEqual(classify_sku_suffix("QW"), "regional")
        self.assertEqual(classify_sku_suffix("12"), "regional")

    def test_real_variants_are_not_merged_with_the_base_model(self):
        self.assertEqual(sku_relation("DCD796P2", "dcd796p2t-gb/drill").kind, "different_variant")
        self.assertEqual(sku_relation("DCD796P2", "dcd796d2-gb/drill").kind, "absent")
        self.assertEqual(sku_relation("Sonicare HX9992/12", "/c-p/HX9992_21/x").kind, "different_suffix")
        self.assertEqual(sku_relation("Sonicare HX9992/12", "/c-p/HX9992/sonicare-brush").kind, "base_only")
        self.assertFalse(sku_relation("DCD796P2", "dcd796p2t").same_model)
        self.assertEqual(classify_sku_suffix("XL"), "variant")

    def test_regional_suffix_accepted_by_relevance_gate_but_variant_rejected(self):
        def searcher(query):
            if query.endswith("official website"):
                return [("https://acme.co.uk/", "Acme official website")]
            return [
                ("https://acme.co.uk/en-gb/product/x100p2-gb/drill", "Acme X100P2-GB drill"),
                ("https://acme.co.uk/en-gb/product/x100p2t-gb/drill", "Acme X100P2T-GB drill"),
                ("https://acme.co.uk/en-gb/product/x100d2-gb/drill", "Acme X100D2-GB drill"),
            ]

        outcome = discover_with_status("Acme", "X100P2", searcher=searcher)
        accepted = {item["url"]: item for item in outcome.candidates}
        self.assertEqual(accepted["https://acme.co.uk/en-gb/product/x100p2-gb/drill"]["relevance_relation"], "exact")
        self.assertEqual(accepted["https://acme.co.uk/en-gb/product/x100p2-gb/drill"]["sku_suffix"], "GB")
        rejected = {item["url"] for item in outcome.rejected_candidates}
        self.assertIn("https://acme.co.uk/en-gb/product/x100p2t-gb/drill", rejected)
        self.assertIn("https://acme.co.uk/en-gb/product/x100d2-gb/drill", rejected)


class DomainAndQueryTests(unittest.TestCase):
    def test_registrable_domain_handles_generic_country_second_level(self):
        self.assertEqual(_registrable_domain("www.philips.com.ge"), "philips.com.ge")
        self.assertEqual(_registrable_domain("shop.brand.co.za"), "brand.co.za")
        self.assertEqual(_registrable_domain("global.dreametech.com"), "dreametech.com")

    def test_brand_roots_cover_regional_suffixes_without_a_brand_table(self):
        roots = _brand_root_domains("Gressel", "global")
        self.assertIn("gressel.com", roots)
        self.assertIn("gressel.ru", roots)
        self.assertIn("gressel.co.uk", roots)
        self.assertEqual(_brand_root_domains("Ab", "global"), [])

    def test_site_operator_becomes_a_plain_domain_keyword_for_duckduckgo(self):
        self.assertEqual(
            strip_search_operators('"HHR32A" site:dreametech.com -site:ca.dreametech.com'),
            '"HHR32A" dreametech.com',
        )
        self.assertEqual(
            build_provider_query("duckduckgo_html", '"X1" site:acme.com', "Acme", "X1"),
            '"X1" acme.com',
        )
        self.assertIn("site:acme.com", build_provider_query("bing", '"X1" site:acme.com', "Acme", "X1"))

    def test_url_variants_toggle_trailing_slash_only_for_page_paths(self):
        self.assertEqual(url_variants("https://a.ru/catalog/x"), ["https://a.ru/catalog/x", "https://a.ru/catalog/x/"])
        self.assertEqual(url_variants("https://a.ru/catalog/x/")[1], "https://a.ru/catalog/x")
        self.assertEqual(url_variants("https://a.ru/f/doc.pdf"), ["https://a.ru/f/doc.pdf"])


class CyrillicAndCanonicalizationTests(unittest.TestCase):
    def setUp(self):
        clear_official_domain_cache()

    def test_cyrillic_title_with_latin_brand_and_model_is_exact(self):
        def searcher(query):
            if query.endswith("official website"):
                return []
            return [(
                "https://acme.ru/catalog/aerogril/aerogril_acme_gaf_1825",
                "Аэрогриль Acme GAF-1825, 9 программ, сенсорный дисплей",
            )]

        outcome = discover_with_status("Acme", "GAF-1825", searcher=searcher)
        self.assertEqual(outcome.candidates[0]["relevance_relation"], "exact")
        self.assertEqual(outcome.candidates[0]["authority_status"], "verified")

    def test_sibling_model_in_a_catalog_list_is_rejected_not_dropped(self):
        def searcher(query):
            if query.endswith("official website"):
                return []
            return [
                ("https://acme.ru/catalog/x/acme_gaf_1825", "Acme GAF-1825"),
                ("https://acme.ru/catalog/x/acme_gaf_1826", "Acme GAF-1826"),
            ]

        outcome = discover_with_status("Acme", "GAF-1825", searcher=searcher)
        self.assertEqual({item["url"] for item in outcome.rejected_candidates},
                         {"https://acme.ru/catalog/x/acme_gaf_1826"})
        self.assertEqual(
            {entry.canonical_url for entry in outcome.trace.entries if entry.outcome == "rejected"},
            {"https://acme.ru/catalog/x/acme_gaf_1826"},
        )

    def test_trailing_slash_only_site_is_fetched_via_the_working_spelling(self):
        seen = []

        class Session:
            def get(self, url, **_kwargs):
                seen.append(url)
                status = 200 if url.endswith("/") else 404
                return FakeResponse(url, "<title>ok</title>", status)

        page = fetch_working_page("https://acme.ru/catalog/x_1825", session=Session())
        self.assertEqual(page[0], "https://acme.ru/catalog/x_1825/")
        self.assertEqual(seen, ["https://acme.ru/catalog/x_1825", "https://acme.ru/catalog/x_1825/"])


class ProviderResilienceTests(unittest.TestCase):
    def setUp(self):
        clear_official_domain_cache()

    class Provider:
        def __init__(self, name, responses):
            self.name = name
            self.responses = list(responses)
            self.calls = []

        def search(self, query):
            self.calls.append(query)
            response = self.responses.pop(0) if self.responses else []
            if isinstance(response, Exception):
                raise response
            return response

    def test_blocked_provider_is_half_opened_after_cooldown(self):
        provider = self.Provider("ddg", [
            RuntimeError("DuckDuckGo bot-check blocked the search."),
            [("https://acme.com/product/x100", "Acme X100")],
        ])
        config = DiscoveryRuntimeConfig(blocked_cooldown_seconds=0.05)
        session = ResilientSearchSession(providers=(provider,), config=config)
        session.configure_identity("Acme", "X100")
        first = session.search_with_status("Acme X100")
        self.assertEqual(first.attempts[0].status, "blocked")
        immediately = session.search_with_status('"Acme X100"')
        self.assertEqual(immediately.attempts[0].status, "circuit_open")
        time.sleep(0.08)
        later = session.search_with_status("Acme X100 specs")
        self.assertEqual(later.attempts[0].status, "success")
        self.assertEqual(len(provider.calls), 2)

    def test_timeout_stays_open_for_the_request(self):
        provider = self.Provider("slow", [TimeoutError("timed out")])
        session = ResilientSearchSession(
            providers=(provider,), config=DiscoveryRuntimeConfig(blocked_cooldown_seconds=0.01),
        )
        session.configure_identity("Acme", "X100")
        session.search_with_status("Acme X100")
        time.sleep(0.03)
        self.assertEqual(session.search_with_status("Acme X100 again").attempts[0].status, "circuit_open")

    def test_fallback_provider_answers_when_primary_fails(self):
        primary = self.Provider("primary", [RuntimeError("provider blocked")])
        fallback = self.Provider("fallback", [[("https://acme.com/product/x100", "Acme X100")]])
        session = ResilientSearchSession(providers=(primary, fallback))
        session.configure_identity("Acme", "X100")
        outcome = session.search_with_status("Acme X100")
        self.assertEqual([a.status for a in outcome.attempts], ["blocked", "success"])
        self.assertEqual(len(outcome.results), 1)

    def test_every_provider_failure_stays_visible_in_the_result(self):
        def searcher(query):
            from core.discovery import ProviderQueryOutcome
            return ProviderQueryOutcome((), (
                ProviderAttempt("ddg", query, "blocked", message="bot-check"),
                ProviderAttempt("naver", query, "timeout", message="slow"),
            ))

        outcome = discover_with_status("Acme", "X100", searcher=searcher)
        result = _result_from_outcome("Acme X100", "Acme", "X100", "global", outcome, 0.1)
        self.assertEqual(result.status, "FAIL")
        self.assertTrue({item["status"] for item in result.provider_failures} >= {"blocked", "timeout"})
        self.assertEqual(len(result.trace["provider_errors"]), len(result.provider_failures))


class DirectProbeAuthorityTests(unittest.TestCase):
    def setUp(self):
        clear_official_domain_cache()

    def test_page_confirmed_brand_verifies_a_title_that_omits_the_brand(self):
        def searcher(query):
            return [SearchResultRecord(
                "https://acme.co.uk/en-gb/product/x100p2-gb/drill", "18V Brushless Drill",
                snippet="Requested SKU confirmed in fetched official page content; "
                        "brand confirmed in fetched page.",
                provider="direct_domain_probe",
            )]

        outcome = discover_with_status("Acme", "X100P2", searcher=searcher, early_official_stop=True)
        item = outcome.candidates[0]
        self.assertEqual(item["authority_status"], "verified")
        self.assertEqual(item["relevance_relation"], "exact")
        self.assertEqual(outcome.attempted_queries[:1], outcome.queries[:1])
        self.assertLess(len(outcome.attempted_queries), len(outcome.queries))  # stopped early

    def test_the_same_title_from_a_search_engine_does_not_verify_authority(self):
        def searcher(query):
            if query.endswith("official website"):
                return []
            return [SearchResultRecord(
                "https://acme.co.uk/en-gb/product/x100p2-gb/drill", "18V Brushless Drill",
                snippet="brand confirmed in fetched page.", provider="some_serp",
            )]

        outcome = discover_with_status("Acme", "X100P2", searcher=searcher)
        self.assertNotEqual(outcome.candidates[0]["authority_status"], "verified")

    def test_regional_suffix_page_short_circuits_lower_priority_providers(self):
        class Direct:
            name = "direct_domain_probe"
            quality_gate = False
            always_run = False
            short_circuit_on_exact_model = True

            def search(self, query):
                return [SearchResultRecord(
                    "https://acme.co.uk/en-gb/product/x100p2-gb/drill", "Acme X100P2-GB drill",
                    provider=self.name,
                )]

        class Serp:
            name = "serp"
            calls = 0

            def search(self, query):
                Serp.calls += 1
                return []

        session = ResilientSearchSession(providers=(Direct(), Serp()))
        session.configure_identity("Acme", "X100P2")
        outcome = session.search_with_status("Acme X100P2")
        self.assertEqual([attempt.provider for attempt in outcome.attempts], ["direct_domain_probe"])
        self.assertTrue(outcome.attempts[0].exact_model_hit)
        self.assertEqual(Serp.calls, 0)


class DirectProbeTests(unittest.TestCase):
    def _provider(self, pages):
        http = requests.Session()
        provider = DirectDomainProbeProvider("global", session=http)
        provider.configure_identity("Acme", "X-100")

        def norm(value):
            return value.replace("/?", "?").rstrip("/")

        def get(url, **_kwargs):
            for prefix, (final, text) in pages.items():
                if norm(url) == norm(prefix):
                    return FakeResponse(final, text)
            return FakeResponse(url, "", 404)

        return provider, patch.object(http, "get", side_effect=get)

    def test_apex_only_regional_domain_is_probed_and_found_by_site_search(self):
        home = (
            '<html><head><title>Acme shop</title></head><body>'
            '<form action="/catalog/"><input name="q"></form></body></html>'
        )
        results_page = '<a href="/catalog/kitchen/acme_x_100/">Acme X-100 air fryer</a>'
        pages = {
            "https://acme.ru/": ("https://acme.ru/", home),
            "https://acme.ru/catalog/?q=X-100": ("https://acme.ru/catalog/?q=X-100", results_page),
            "https://acme.ru/catalog/kitchen/acme_x_100/": (
                "https://acme.ru/catalog/kitchen/acme_x_100/",
                "<html><title>Acme X-100 air fryer</title></html>",
            ),
        }
        provider, patched = self._provider(pages)
        with patched:
            records = provider.search_with_timeout('"X-100" Acme', 10)
        self.assertIn("https://acme.ru/catalog/kitchen/acme_x_100", [item.url for item in records])

    def test_learned_product_path_template_finds_slash_sku_page(self):
        provider = DirectDomainProbeProvider("global", session=requests.Session())
        provider.configure_identity("Acme", "Toothbrush HX9992/12")
        text = (
            '<a href="/c-p/HX9990_11/brush-a">a</a><a href="/c-p/HX9994_12/brush-b">b</a>'
            '<img src="/v3/assets/blt123abc456/x.png">'
        )
        prefixes = provider._learn_sku_prefixes([text], "acme.com")
        self.assertEqual(prefixes, ["/c-p/"])

        def get(url, deadline, method):
            from core.discovery import _FetchedOfficialSurface
            if url.endswith("/c-p/HX9992_12"):
                return _FetchedOfficialSurface(
                    url, 200, "<title>Toothbrush HX9992/12 | Acme</title>", "text/html",
                )
            return None

        provider._get = get  # type: ignore[method-assign]
        records = provider._sku_template_records(
            prefixes, "https://www.acme.com", "acme.com", time.monotonic() + 5,
        )
        self.assertEqual([item.discovery_method for item in records], ["sku_path_template"])
        self.assertIn("HX9992_12", records[0].url)


class DocumentClassificationTests(unittest.TestCase):
    def test_manual_detection_is_multilingual(self):
        for text in ("User Manual", "Инструкция GAF-1825", "Bedienungsanleitung", "Руководство по эксплуатации"):
            self.assertIn(classify_document("https://a.com/f.pdf", text), {"manual", "instruction"}, text)
        self.assertEqual(classify_document("https://a.com/x.pdf", "User guide"), "user_guide")
        self.assertEqual(classify_document("https://a.com/x.pdf", "Quick start guide"), "quick_start_guide")

    def test_datasheet_and_spec_sheet_detection(self):
        self.assertEqual(classify_document("https://a.com/x.pdf", "Product datasheet"), "datasheet")
        self.assertEqual(classify_document("https://a.com/x-data-sheet.pdf", ""), "datasheet")
        self.assertEqual(classify_document("https://a.com/x.pdf", "Spec sheet"), "spec_sheet")

    def test_declaration_certificate_and_safety_detection(self):
        self.assertEqual(classify_document("https://a.com/f/DOC-W2545E_HHR32A.pdf", ""), "declaration")
        self.assertEqual(classify_document("https://a.com/x.pdf", "EU Declaration of Conformity"), "declaration")
        self.assertEqual(classify_document("https://a.com/x.pdf", "Certificate"), "certificate")
        self.assertEqual(classify_document("https://a.com/x.pdf", "Safety data sheet"), "safety_document")
        self.assertIsNone(classify_document("https://a.com/product/x100", "Acme X100 drill"))

    def test_canonical_fields_are_generic_identifiers(self):
        docs = [
            OfficialDocument("https://a.com/m.pdf", "manual", "manual_url", "m", "official", "exact", "r"),
            OfficialDocument("https://a.com/g.pdf", "user_guide", "manual_url", "g", "official", "exact", "r"),
            OfficialDocument("https://a.com/d.pdf", "declaration", "declaration_url", "d", "official", "exact", "r"),
            OfficialDocument("https://a.com/q.pdf", "quick_start_guide", "quick_start_guide_url", "q", "official", "exact", "r"),
        ]
        fields = documents_by_canonical_field(docs)
        self.assertEqual(fields["manual_url"], ["https://a.com/m.pdf", "https://a.com/g.pdf"])
        self.assertEqual(fields["declaration_url"], ["https://a.com/d.pdf"])
        for name in ("datasheet_url", "safety_document_url", "certificate_url"):
            self.assertEqual(fields[name], [])

    def test_duplicate_documents_merge_across_pages_and_cache_busters(self):
        first = OfficialDocument(
            "https://cdn.acme.com/f/DOC-X100.pdf?v=1", "declaration", "declaration_url", "DoC",
            "official", "probable", "r", "https://acme.com/a", ("https://acme.com/a",),
        )
        second = OfficialDocument(
            "https://cdn.acme.com/f/DOC-X100.pdf?v=2", "declaration", "declaration_url", "DoC X100",
            "official", "exact", "r", "https://acme.com/b", ("https://acme.com/b",),
        )
        self.assertEqual(canonical_document_url(first.url), canonical_document_url(second.url))
        merged = merge_documents([first, second])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].model_match, "exact")
        self.assertEqual(set(merged[0].found_on), {"https://acme.com/a", "https://acme.com/b"})

    def test_extraction_links_only_model_documents_from_official_pages(self):
        html = """
        <a href="/upload/manual.pdf">Инструкция GAF-1825</a>
        <a href="/upload/other.pdf">Инструкция GAF-1826</a>
        <a href="https://cdn.other.net/DOC_HHR10D.pdf">Declaration HHR10D</a>
        <a href="https://reseller.example/declaration">Declaration of Performance</a>
        <a href="/pages/user-manuals-and-faqs">User Manuals and FAQs</a>
        """
        accepted, rejected = extract_documents(
            html, "https://gressel.ru/catalog/x/", brand="Gressel", model="GAF-1825",
            source_authority="official", source_is_exact_page=False,
        )
        self.assertEqual([item.url for item in accepted], ["https://gressel.ru/upload/manual.pdf"])
        self.assertEqual(accepted[0].model_match, "exact")
        reasons = " | ".join(item.reason for item in rejected)
        self.assertIn("not identified", reasons)
        self.assertIn("non-official host", reasons)
        self.assertIn("hub/index", reasons)

    def test_same_family_but_different_sku_document_is_rejected(self):
        html = '<a href="https://cdn.x.com/DOC_HHR10D.pdf">Dreame G12 Pro Flex - HHR10D declaration</a>'
        accepted, rejected = extract_documents(
            html, "https://global.acme.com/pages/doc", brand="Dreame", model="G12 Pro HHR32A",
            source_authority="official", source_is_exact_page=False,
        )
        self.assertEqual(accepted, [])
        self.assertIn("different model identifier", rejected[0].reason)

    def test_files_on_an_exact_product_page_inherit_its_model_evidence(self):
        html = '<a href="https://docs.acme.com/assets/0a1b2c.pdf">Declaration of Conformity</a>'
        accepted, _ = extract_documents(
            html, "https://acme.co.uk/c-p/HX9992_12/x", brand="Acme",
            model="Sonicare HX9992/12", source_authority="official",
            source_is_exact_page=True,
        )
        self.assertEqual(accepted[0].model_match, "exact")
        self.assertIn("exact official product page", accepted[0].reason)


class EmbeddedDocumentStateTests(unittest.TestCase):
    STATE = (
        '{\\"docs\\":[{\\"description\\":\\"User manual\\",\\"type\\":\\"DFU\\",\\"code\\":\\"HX9992_12\\",'
        '\\"lang\\":\\"DEU\\",\\"asset\\":\\"https:\u002F\u002Fdocs.acme.com\u002Fassets\u002Faaa111.pdf\\"},'
        '{\\"description\\":\\"User manual\\",\\"type\\":\\"DFU\\",\\"code\\":\\"HX9992_12\\",'
        '\\"lang\\":\\"ENG\\",\\"asset\\":\\"https:\u002F\u002Fdocs.acme.com\u002Fassets\u002Fbbb222.pdf\\"}]}'
    )

    def test_escaped_json_state_documents_are_labelled_and_locale_collapsed(self):
        html = f"<script>{self.STATE}</script>"
        accepted, _ = extract_documents(
            html, "https://acme.co.uk/c-p/HX9992_12/x", brand="Acme", model="Sonicare HX9992/12",
            source_authority="official", source_is_exact_page=False,
        )
        self.assertEqual(len(accepted), 1)
        self.assertEqual(accepted[0].doc_type, "manual")
        self.assertTrue(accepted[0].url.endswith("bbb222.pdf"))  # English preferred
        self.assertEqual(set(accepted[0].locales), {"DEU", "ENG"})
        self.assertIn("other locale", accepted[0].reason)

    def test_part_numbers_are_not_mistaken_for_a_conflicting_model(self):
        html = '<a href="/files/W2545G-EN.pdf">G12 Pro User Manual</a>'
        accepted, rejected = extract_documents(
            html, "https://global.acme.com/pages/g12-pro", brand="Dreame", model="G12 Pro HHR32A",
            source_authority="official", source_is_exact_page=False, source_is_probable_page=True,
        )
        self.assertEqual([item.model_match for item in accepted], ["probable"])
        self.assertEqual(rejected, [])


class PageInspectionTests(unittest.TestCase):
    ROWS = "".join(f"<tr><td>Param {i}</td><td>{i}</td></tr>" for i in range(6))

    def test_expandable_spec_control_with_content_in_dom(self):
        html = (
            '<a href="#all-props"><span>Все характеристики</span></a>'
            f'<div class="tab-pane" id="props"><table class="properties">{self.ROWS}</table></div>'
        )
        result = inspect_product_page(html)
        self.assertTrue(result.has_expandable_specs)
        self.assertEqual(result.has_hidden_spec_content, "true")
        self.assertEqual(result.requires_interaction, "false")
        self.assertIn("Все характеристики", result.spec_controls)

    def test_display_none_spec_block_is_hidden_dom_content(self):
        html = f'<div class="specs" style="display: none"><table>{self.ROWS}</table></div>'
        result = inspect_product_page(html)
        self.assertEqual(result.has_hidden_spec_content, "true")
        self.assertEqual(result.requires_interaction, "false")
        self.assertGreater(result.spec_rows_hidden, 0)

    def test_hidden_attribute_details_and_template(self):
        for wrapper in (
            f'<div class="specifications" hidden><table>{self.ROWS}</table></div>',
            f'<details class="specs"><summary>Specs</summary><table>{self.ROWS}</table></details>',
            f'<template class="specs"><table>{self.ROWS}</table></template>',
        ):
            self.assertEqual(inspect_product_page(wrapper).has_hidden_spec_content, "true", wrapper)

    def test_json_state_counts_as_content_present_in_html(self):
        html = (
            '<button aria-expanded="false">Show all specifications</button>'
            '<script type="application/json">{"specifications": [{"name": "Power", "value": "1"}]}</script>'
        )
        result = inspect_product_page(html)
        self.assertTrue(result.has_expandable_specs)
        self.assertEqual(result.has_hidden_spec_content, "true")
        self.assertEqual(result.requires_interaction, "false")

    def test_control_without_any_spec_content_requires_interaction(self):
        html = '<button aria-controls="specs">Show all specifications</button><div id="root2"></div>'
        result = inspect_product_page(html)
        self.assertTrue(result.has_expandable_specs)
        self.assertEqual(result.requires_interaction, "true")

    def test_plain_page_has_no_expandable_specs(self):
        html = f'<h1>Drill</h1><table class="specs">{self.ROWS}</table>'
        result = inspect_product_page(html)
        self.assertFalse(result.has_expandable_specs)
        self.assertEqual(result.has_hidden_spec_content, "false")
        self.assertEqual(result.requires_interaction, "false")

    def test_js_shell_is_reported_unknown_not_guessed(self):
        result = inspect_product_page('<div id="__next"></div><script>window.x=1</script>')
        self.assertEqual(result.requires_interaction, "unknown")
        self.assertEqual(result.has_hidden_spec_content, "unknown")


class DocumentQueryTests(unittest.TestCase):
    def test_document_queries_use_the_sku_and_only_official_domains(self):
        queries = document_queries("Philips", "Sonicare 9900 Prestige HX9992/12", ["philips.co.uk", "philips.de", "x.com"])
        self.assertIn('"HX9992" manual site:philips.co.uk', queries)
        self.assertIn('"HX9992" manual site:philips.de', queries)
        self.assertFalse(any("x.com" in query for query in queries))
        self.assertTrue(any("declaration of conformity" in query for query in queries))

    def test_without_a_verified_domain_the_query_is_brand_scoped(self):
        queries = document_queries("Acme", "X100", [])
        self.assertEqual(queries[0], 'Acme "X100" user manual pdf')


class BotOutputTests(unittest.TestCase):
    def setUp(self):
        clear_official_domain_cache()

    def test_groups_are_separate_and_links_are_shown(self):
        def searcher(query):
            if query.endswith("official website"):
                return [("https://acme.com/", "Acme official website")]
            return [("https://acme.com/product/x100", "Acme X100 drill")]

        outcome = discover_with_status("Acme", "X100", searcher=searcher)
        html = (
            '<a href="/docs/x100-manual.pdf">X100 user manual</a>'
            '<button aria-expanded="false">Show all specifications</button>'
        )
        result = _result_from_outcome(
            "Acme X100", "Acme", "X100", "global", outcome, 0.1,
            fetch=lambda url: (url, html),
        )
        self.assertEqual(result.status, "PASS")
        self.assertEqual([item.url for item in result.official_pages], ["https://acme.com/product/x100"])
        self.assertEqual([doc.doc_type for doc in result.documents], ["manual"])
        self.assertEqual(result.document_fields["manual_url"], ["https://acme.com/docs/x100-manual.pdf"])
        self.assertTrue(result.discovery_metadata["has_expandable_specs"])
        text = "\n".join(format_discovery_result(result))
        for heading in ("Official product pages", "Official support pages", "Official documents",
                        "Secondary", "Discovery metadata"):
            self.assertIn(heading, text)
        self.assertIn("Manual — https://acme.com/docs/x100-manual.pdf", text)
        self.assertIn("expandable specs: yes", text)
        self.assertLess(text.index("Official product pages"), text.index("Official documents"))
        self.assertNotIn("https://acme.com/docs/x100-manual.pdf",
                         text[text.index("Official product pages"):text.index("Official documents")])

    def test_document_page_is_not_reported_as_a_product_page(self):
        def searcher(query):
            if query.endswith("official website"):
                return [("https://acme.com/", "Acme official website")]
            return [("https://acme.com/pages/x100-user-manual", "X100 User Manual – Acme")]

        outcome = discover_with_status("Acme", "X100", searcher=searcher)
        result = _result_from_outcome("Acme X100", "Acme", "X100", "global", outcome, 0.1)
        self.assertEqual(result.official_pages, ())
        self.assertEqual(result.status, "PARTIAL")
        self.assertEqual(result.documents[0].doc_type, "manual")


if __name__ == "__main__":
    unittest.main()
