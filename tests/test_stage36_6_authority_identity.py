"""Stage 36.6 gates shared by discovery, debug, extraction, and workflow."""

import unittest
import json
from datetime import date
from pathlib import Path
from unittest.mock import patch

from core.authority import TrustedSource, resolve_authority
from core.authority_registry import find_seed
from core.discovery import DiscoveryOutcome, assess_candidate_relevance, canonicalize_url, rank_candidates
from core.identity import assess_product_page_identity
from core.official_documents import OfficialDocument
from core.workflow import ProductWorkflowRequest, _resolve_authority_roles, run_product_workflow
from diagnostics.stage36_5_baseline import PRODUCTS
from diagnostics.stage36_6_saved_replay import replay as replay_saved_losses
from diagnostics.stage36_6_public_replay import SavedProvider, replay as replay_public_losses, replay_accepted, replay_negatives
from diagnostics.stage36_6_saved_replay import PRIMARY
from diagnostics.stage36_6_transition_audit import CONFIRMED, _key
from diagnostics.stage36_5_baseline import load_archive
from bot.handlers import handle_discovery_query
from core.discovery import clear_official_domain_cache
from services.discovery_debug import DiscoveryDebugService
from services.discovery_debug import DiscoverySource, _candidate_view, _result_from_outcome
from services.raw_extraction import RawExtractionService
from tests.test_workflow import FixtureServices, candidate


class AuthorityContractTests(unittest.TestCase):
    def test_seed_is_brand_host_and_time_scoped(self):
        self.assertIsNotNone(find_seed("Razer", "www.razer.com", today=date(2026, 9, 20), category="computer/peripherals"))
        self.assertIsNone(find_seed("Razer", "community.razer.com", today=date(2026, 9, 20), category="computer/peripherals"))
        self.assertIsNone(find_seed("Another", "razer.com", today=date(2026, 9, 20), category="computer/peripherals"))
        self.assertIsNone(find_seed("Razer", "razer.com", today=date(2028, 9, 20), category="computer/peripherals"))
        distributor = find_seed("TP-Link", "tp-link.cz", today=date(2026, 9, 20), category="networking")
        self.assertEqual(distributor.operator_relation, "independent_distributor")
        self.assertFalse(distributor.first_party)
        self.assertTrue(distributor.evidence_excerpt)

    def test_search_self_claim_is_provisional(self):
        page = rank_candidates(
            [("https://acme.example/product/X100", "Acme X100 official product")],
            "Acme", "X100", official_domains={"acme.example": "https://acme.example/"},
        )[0]
        self.assertEqual(page["authority_status"], "provisional")
        self.assertEqual(page["authority_evidence_kind"], "search_hypothesis")

    def test_audited_distributor_does_not_become_first_party(self):
        page = rank_candidates(
            [("https://www.tp-link.cz/cs/296056-tp-link-deco-be85-2ks", "TP-Link Deco BE85 2-pack")],
            "TP-Link", "Deco BE85", product_category="networking",
        )[0]
        self.assertEqual(page["source_type"], "distributor")
        self.assertEqual(page["authority_status"], "verified")
        self.assertEqual(_candidate_view(page).group, "dealer")

    def test_reviewed_scope_is_a_decision_gate(self):
        self.assertIsNone(find_seed("Bosch", "bosch-professional.com"))
        self.assertIsNone(find_seed("Bosch", "bosch-professional.com", category="major appliances"))
        self.assertIsNone(find_seed("Philips", "home-appliances.philips", category="personal care/skincare"))
        self.assertIsNone(find_seed("TP-Link", "tp-link.cz", category="power tools"))
        self.assertIsNone(find_seed("Bosch", "bosch-home.co.uk", category="power tools"))
        page = rank_candidates(
            [("https://www.bosch-professional.com/product/WAN28254GB", "Bosch WAN28254GB washing machine")],
            "Bosch", "WAN28254GB", product_category="major appliances",
        )[0]
        self.assertNotEqual(page["authority_status"], "verified")

    def test_unconfirmed_operator_seed_never_grants_first_party(self):
        for brand, host, model, category in (
            ("Frostbite", "fishfrostbite.com", "Drench 39ML", "fishing"),
            ("Nautilus", "nautilusreels.com", "X Series XL MAX", "fishing"),
        ):
            with self.subTest(brand=brand):
                seed = find_seed(brand, host, category=category)
                self.assertFalse(seed.first_party)
                page = rank_candidates(
                    [(f"https://{host}/products/{model.replace(' ', '-').lower()}", f"{brand} {model}")],
                    brand, model, product_category=category,
                )[0]
                self.assertNotEqual(page["authority_status"], "verified")

    def test_workflow_rechecks_scope_after_fetch(self):
        def fetched():
            return {
                "source_url": "https://www.bosch-professional.com/product/WAN28254GB",
                "final_url": "https://www.bosch-professional.com/product/WAN28254GB",
                "status": "success", "document_type": "html", "source_type": "manufacturer",
                "authority_status": "verified", "authority_evidence_kind": "audited_registry",
                "html": "<h1>Bosch WAN28254GB washing machine</h1>", "text": "Bosch washing machine",
            }
        wrong = fetched()
        _resolve_authority_roles([wrong], "Bosch", "major appliances")
        self.assertEqual(wrong["authority_status"], "unknown")
        allowed = fetched()
        _resolve_authority_roles([allowed], "Bosch", "power tools")
        self.assertEqual(allowed["authority_status"], "verified")

    def test_generic_link_does_not_establish_ownership_or_dealer_role(self):
        source = TrustedSource("acme.example", '<a href="https://acme-shop.example/">Where to buy</a>')
        html = "<h1>Acme X100</h1><p>Official Acme distributor</p>"
        result = resolve_authority(html, "Official Acme distributor", "acme-shop.example", "Acme", (source,))
        self.assertEqual(result.role, "unknown")

    def test_contextual_anchor_proves_only_its_named_relation(self):
        source = TrustedSource("acme.example", '<a href="https://shop.example/">Authorized dealer</a>', url="https://acme.example/dealers")
        result = resolve_authority("<p>Authorized dealer of Acme</p>", "Authorized dealer of Acme", "shop.example", "Acme", (source,))
        self.assertEqual(result.role, "authorized_dealer")
        self.assertEqual(result.corroboration.evidence_url, source.url)
        manufacturer_claim = resolve_authority(
            '<script type="application/ld+json">{"@type":"Organization","name":"Acme"}</script>',
            "", "shop.example", "Acme", (source,),
        )
        self.assertEqual(manufacturer_claim.role, "unknown")


class ProductIdentityTests(unittest.TestCase):
    def check_page(self, model, heading, url, expected):
        decision = assess_product_page_identity(model, f"<h1>{heading}</h1>", url)
        self.assertEqual(decision.relation, expected, heading)

    def test_main_object_not_incidental_compatibility(self):
        self.check_page("S8 MaxV Ultra", "Dust bag for S8 MaxV Ultra", "https://us.roborock.com/products/dust-bag-for-s8-maxv-ultra", "related_item")

    def test_variants_and_package_forms(self):
        self.check_page("DeathAdder V3", "Razer DeathAdder V3 HyperSpeed", "https://razer.com/gaming-mice/razer-deathadder-v3-hyperspeed", "different_variant")
        self.check_page("RM850x", "RM850x SHIFT Fully Modular", "https://corsair.com/p/rm850x-shift", "different_variant")
        self.check_page("Hydrating Facial Cleanser", "Hydrating Facial Cleanser Refill", "https://cerave.com/hydrating-facial-cleanser-refill", "different_variant")
        self.check_page("iO Series 10", "iO Series 10 Twin Pack", "https://oralb.com/io-series-10-twin-pack", "different_variant")
        self.check_page("MX Keys S", "MX Keys S Combo", "https://logitech.com/en-us/shop/p/mx-keys-s-combo", "different_variant")
        self.check_page("MX Keys S", "MX Keys S", "https://logitech.com/en-us/shop/p/mx-keys-s-combo", "different_variant")

    def test_content_can_prove_numeric_url(self):
        self.check_page("SN23EI03ME", "Siemens SN23EI03ME", "https://siemens-home.bsh-group.com/product/12345", "exact")

    def test_spaced_complete_sku_in_primary_heading_and_url(self):
        url = "https://braunhousehold.com/en/p/multiquick-9-hand-blender-mq-9187xli/HB901-MQ9187XLI.html"
        self.check_page("MultiQuick 9 MQ9187XLI", "MultiQuick 9 Hand blender MQ 9187XLI", url, "exact")
        self.check_page("MultiQuick 9 MQ9187XLI", "MultiQuick 9 Hand blender MQ 9187XLII", url, "unknown")

    def test_unique_product_code_can_complete_descriptive_heading(self):
        html = ('<h1>Multifunction oven</h1><script type="application/ld+json">'
                '{"@type":"Product","name":"Multifunction oven", "sku":"EOD6P77WX"}'
                '</script>')
        url = "https://electrolux.bg/kitchen/cooking/ovens/oven/eod6p77wx"
        self.assertEqual(assess_product_page_identity("EOD6P77WX", html, url).relation, "exact")
        self.assertEqual(assess_product_page_identity("EOD6P77WY", html, url).relation, "different_variant")
        accessory = html.replace("Multifunction oven", "Dust bag for EOD6P77WX")
        self.assertEqual(assess_product_page_identity("EOD6P77WX", accessory, url).relation, "related_item")

    def test_review_score_after_sku_is_not_variant(self):
        url = "https://bosch-home.co.uk/en/product/WAN28254GB"
        self.check_page("WAN28254GB", "Series 4 Washing machine WAN28254GB 4.7 (265) Questions & answers", url, "exact")
        self.check_page("WAN28254GB", "Series 4 Washing machine WAN28254GB 16GB", url, "unknown")
        self.check_page("GA023GZ", "GA023GZ 40Vmax XGT Brushless Angle Grinder", "https://makita.co.nz/products/model/GA023GZ", "exact")

    def test_single_labelled_style_can_complete_family_heading(self):
        html = "<main><h1>Nike AeroSwift</h1><p>Style: FN4231-010</p></main>"
        url = "https://nike.com/dk/en/t/aeroswift/FN4231-010"
        self.assertEqual(assess_product_page_identity("AeroSwift FN4231-010", html, url).relation, "exact")
        self.assertNotEqual(assess_product_page_identity(
            "AeroSwift FN4231-010", html.replace("FN4231-010", "FN4231-011"), url,
        ).relation, "exact")
        self.assertNotEqual(assess_product_page_identity(
            "AeroSwift FN4231-010", html.replace("</main>", "<p>Style: FN4231-011</p></main>"), url,
        ).relation, "exact")

    def test_regional_suffix_requires_commercial_evidence(self):
        self.check_page("X100P2", "Acme X100P2-GB", "https://acme.example/x100p2-gb", "unknown")

    def test_debug_route_revokes_old_exact_and_extraction_cannot_receive_it(self):
        url = "https://razer.com/gaming-mice/razer-deathadder-v3-hyperspeed"
        items = rank_candidates([(url, "Razer DeathAdder V3 HyperSpeed")], "Razer", "DeathAdder V3", official_domains={"razer.com": "https://razer.com/"})
        outcome = DiscoveryOutcome(items, "success", [], [], [])
        result = _result_from_outcome(
            "Razer DeathAdder V3", "Razer", "DeathAdder V3", "global", outcome, 0.1,
            fetch=lambda page_url: (page_url, "<h1>Razer DeathAdder V3 HyperSpeed</h1>"),
        )
        self.assertFalse(result.exact_official_found)
        self.assertEqual(result.official_pages, ())

    def test_js_shell_keeps_known_operator_but_not_exact_identity(self):
        url = "https://www.razer.com/gaming-mice/razer-deathadder-v3"
        items = rank_candidates([(url, "Razer DeathAdder V3")], "Razer", "DeathAdder V3", product_category="computer/peripherals")
        outcome = DiscoveryOutcome(items, "success", [], [], [])
        result = _result_from_outcome(
            "Razer DeathAdder V3", "Razer", "DeathAdder V3", "global", outcome, 0.1,
            fetch=lambda page_url: (page_url, "<html><title>App</title></html>"),
            product_category="computer/peripherals",
        )
        self.assertFalse(result.exact_official_found)
        self.assertEqual(result.status, "PARTIAL")
        self.assertEqual(result.official_pages[0].model_match, "weak")
        self.assertFalse(result.official_pages[0].content_identity_verified)

    def test_old_verified_search_hypothesis_cannot_bypass_final_gate(self):
        url = "https://acme.example/product/X100"
        old = candidate(url)
        old["authority_evidence_kind"] = "search_hypothesis"
        fixtures = FixtureServices([old], {url: []})
        result = run_product_workflow(
            ProductWorkflowRequest("Acme X100", brand="Acme", targeted_search_enabled=False),
            services=fixtures.services(),
        )
        self.assertEqual(result.fetched_sources[0]["authority_status"], "unknown")

    def test_old_verified_without_provenance_cannot_bypass_final_gate(self):
        url = "https://acme.example/product/X100"
        fixtures = FixtureServices([candidate(url)], {url: []})
        result = run_product_workflow(
            ProductWorkflowRequest("Acme X100", brand="Acme", targeted_search_enabled=False),
            services=fixtures.services(),
        )
        self.assertEqual(result.fetched_sources[0]["authority_status"], "unknown")

    def test_forum_on_brand_host_is_not_editorial_product(self):
        item = rank_candidates(
            [("https://www.netgear.com/discussions/gs308ep", "GS308EP discussion")],
            "NETGEAR", "GS308EP",
        )[0]
        view = _candidate_view(item)
        self.assertEqual(view.page_role, "forum")
        self.assertEqual(view.group, "secondary")

    def test_extraction_preserves_authority_evidence_and_checks_redirect(self):
        source = DiscoverySource(
            "https://www.razer.com/gaming-mice/razer-deathadder-v3", "razer.com",
            "manufacturer", "Razer DeathAdder V3", "exact", "manufacturer", "audited", "official",
            authority_status="verified", authority_evidence_url="https://www.razer.com/gaming-mice/razer-deathadder-v3",
            authority_evidence_kind="audited_registry", authority_evidence_excerpt="Razer product page",
            authority_checked_on="2026-09-20", authority_rules_version=1,
        )
        html = "<h1>Razer DeathAdder V3</h1><table><tr><th>Weight</th><td>59 g</td></tr></table>"
        extraction = RawExtractionService(fetch_html=lambda _: (source.url, html))._extract_page(
            source, "official_product_page", "DeathAdder V3", "Razer",
        )
        self.assertEqual(extraction.authority_evidence_kind, "audited_registry")
        self.assertEqual(extraction.authority_checked_on, "2026-09-20")
        self.assertEqual(extraction.identity_relation, "exact")
        redirected = RawExtractionService(fetch_html=lambda _: ("https://shop.example/item", html))._extract_page(
            source, "official_product_page", "DeathAdder V3", "Razer",
        )
        self.assertEqual(redirected.issues, ("authority_redirect_not_verified",))
        self.assertEqual(redirected.attributes, ())

    def test_extraction_rechecks_support_and_html_document_identity(self):
        url = "https://razer.com/support/deathadder-v3"
        source = DiscoverySource(
            url, "razer.com", "manufacturer", "Razer DeathAdder V3 support",
            "exact", "manufacturer", "audited", "official", page_role="support",
        )
        wrong = "<h1>Razer DeathAdder V3 HyperSpeed</h1><table><tr><th>Weight</th><td>55 g</td></tr></table>"
        service = RawExtractionService(fetch_html=lambda _: (url, wrong))
        support = service._extract_page(source, "official_support_page", "DeathAdder V3", "Razer")
        self.assertEqual(support.issues, ("product_identity_not_verified",))
        self.assertEqual(support.attributes, ())
        document = OfficialDocument(
            url, "manual", "manual_url", "Razer DeathAdder V3 Manual",
            "official", "exact", "fixture", file_type="page",
        )
        html_doc = service._extract_document(document, "DeathAdder V3", "Razer")
        self.assertEqual(html_doc.issues, ("document_identity_not_verified",))
        self.assertEqual(html_doc.attributes, ())


class HeldoutCases(unittest.TestCase):
    def test_public_name_only_replay_recovers_scoped_pages(self):
        outcomes = replay_public_losses()
        self.assertEqual(len(outcomes), 11)
        self.assertTrue(all(row["status"] == "PASS" for row in outcomes), outcomes)

    def test_public_name_only_replay_rechecks_all_ten_old_acceptances(self):
        for explicit in (False, True):
            outcomes = replay_accepted(explicit_category=explicit)
            self.assertEqual(len(outcomes), 10)
            by_index = {row["index"]: row for row in outcomes}
            self.assertEqual({index for index, row in by_index.items() if row["status"] == "PASS"},
                             {2, 7, 12, 14, 23, 33, 50} if explicit else {2, 7, 14, 23, 33, 50})
            self.assertTrue(all(not by_index[index]["exact_official_found"] for index in (19, 43, 44)))

    def test_public_name_only_negative_replay_never_grants_exact_official(self):
        for explicit in (False, True):
            outcomes = replay_negatives(explicit_category=explicit)
            self.assertEqual(len(outcomes), 6)
            self.assertTrue(all(not row["exact_official_found"] for row in outcomes), outcomes)

    def test_public_route_checks_product_scope_not_search_title(self):
        cases = (
            ("TP-Link Deco BE85", "https://www.tp-link.cz/cs/deco-be85", "TP-Link Deco BE85 Mesh Router", "TP-Link Deco BE85 Mesh Router"),
            ("Bosch WAN28254GB", "https://www.bosch-professional.com/product/WAN28254GB", "Bosch WAN28254GB power tools", "Bosch WAN28254GB Washing machine"),
            ("Philips Sonicare HX9992/12", "https://home-appliances.philips/products/HX9992_12", "Philips HX9992/12 air fryer", "Philips Sonicare HX9992/12 electric toothbrush"),
            ("ASUS RT-BE88U", "https://www.asus.com/us/product/rt-be88u", "ASUS RT-BE88U router", "ASUS RT-BE88U electric toothbrush"),
            ("ASUS RT-BE88U", "https://www.asus.com/us/product/rt-be88u", "ASUS RT-BE88U router", "ASUS RT-BE88U"),
        )
        for name, url, search_title, heading in cases:
            with self.subTest(name=name, heading=heading):
                clear_official_domain_cache()
                service = DiscoveryDebugService(providers=lambda: [SavedProvider(url, search_title)],
                                                document_reader=lambda _url: None)
                with patch("services.discovery_debug.fetch_working_page", return_value=(url, f"<h1>{heading}</h1>")):
                    result = service.discover_name(name)
                self.assertFalse(result.exact_official_found)
                self.assertFalse(result.official_pages)
                if name.startswith("TP-Link"):
                    self.assertTrue(result.dealers)
                    self.assertTrue(all(item.source_type != "manufacturer" for item in result.dealers))

    def test_explicit_category_cannot_override_conflicting_primary_product(self):
        cases = (
            ("Bosch WAN28254GB", "https://www.bosch-professional.com/product/WAN28254GB", "power tools", "Bosch WAN28254GB Washing machine"),
            ("Philips Sonicare HX9992/12", "https://home-appliances.philips/products/HX9992_12", "small appliances", "Philips Sonicare HX9992/12 electric toothbrush"),
            ("ASUS RT-BE88U", "https://www.asus.com/us/product/rt-be88u", "networking", "ASUS RT-BE88U electric toothbrush"),
            ("ASUS RT-BE88U", "https://www.asus.com/us/product/rt-be88u", "networking", "ASUS RT-BE88U"),
        )
        for name, url, category, heading in cases:
            with self.subTest(name=name, heading=heading):
                clear_official_domain_cache()
                service = DiscoveryDebugService(providers=lambda: [SavedProvider(url, heading)],
                                                document_reader=lambda _url: None)
                with patch("services.discovery_debug.fetch_working_page", return_value=(url, f"<h1>{heading}</h1>")):
                    result = service.discover_name(name, product_category=category)
                self.assertFalse(result.exact_official_found)
                self.assertFalse(result.official_pages)

    def test_category_is_not_inferred_from_navigation(self):
        from core.identity import assess_product_page_identity
        from core.product_scope import category_from_primary_product
        html = ("<nav>WiFi routers, power supplies, washing machines</nav>"
                "<h1>ASUS RT-BE88U</h1>")
        identity = assess_product_page_identity("RT-BE88U", html, "https://www.asus.com/us/product/rt-be88u")
        self.assertEqual(category_from_primary_product("RT-BE88U", html, identity)[0], "unknown")
        unrelated_product = ("<h1>ASUS RT-BE88U</h1><script type='application/ld+json'>"
                             '{"@type":"Product","name":"Other Router","category":"WiFi Routers"}'
                             "</script>")
        identity = assess_product_page_identity("RT-BE88U", unrelated_product,
                                                "https://www.asus.com/us/product/rt-be88u")
        self.assertEqual(category_from_primary_product("RT-BE88U", unrelated_product, identity)[0], "unknown")

    def test_search_tracking_parameters_do_not_duplicate_product_url(self):
        base = "https://www.corsair.com/ww/en/p/psu/cp-9020270-na/rmx-series-rm850x"
        self.assertEqual(canonicalize_url(base + "?position=4&queryID=abc"), base.replace("www.", ""))

    def test_confirmed_loss_candidate_replay(self):
        outcomes = replay_saved_losses()
        self.assertEqual(len(outcomes), 11)
        self.assertTrue(all(item["authority"] == "verified" for item in outcomes))
        self.assertTrue(all(item["status"] == "PASS" for item in outcomes))

    def test_all_ten_accepted_urls_remain_separately_reviewed(self):
        headings = {
            2: "Siemens SN23EI03ME", 7: "Haier HCR5919EHMB", 12: "Roborock S8 MaxV Ultra",
            14: "MultiQuick 9 Hand blender MQ 9187XLI", 19: "NETGEAR GS308EP",
            23: "Razer DeathAdder V3", 33: "RM850x Fully Modular Power Supply",
            43: "Frostbite Drench 39ML", 44: "Nautilus X Series XL MAX",
            50: "CeraVe Hydrating Facial Cleanser",
        }
        path = Path(__file__).resolve().parents[1] / "diagnostics/baselines/stage36_6/manual_exact_audit.csv"
        import csv
        with path.open(newline="", encoding="utf-8") as handle:
            reviewed = list(csv.DictReader(handle))
        self.assertEqual(len(reviewed), 10)
        for record in reviewed:
            index = int(record["index"])
            brand, model, category, level = PRODUCTS[index - 1]
            url = record["product_url"]
            candidate = rank_candidates([(url, headings[index])], brand, model, product_category=category)[0]
            html = f"<h1>{headings[index]}</h1>"
            if index == 19:
                html = ('<h1>8-Port Gigabit Ethernet PoE+ Easy Smart Essentials Switch (62W)</h1>'
                        '<script type="application/ld+json">{"@type":"Product",'
                        '"name":"8-Port Gigabit Ethernet PoE+ Easy Smart Essentials Switch (62W)",'
                        '"sku":"GS308EP-100NAS","model":"GS308EP"}</script>')
            decision = assess_product_page_identity(model, html, url)
            with self.subTest(index=index, level=level):
                self.assertEqual(decision.relation, "unknown" if index == 19 else "exact")
                if index in {43, 44}:
                    self.assertNotEqual(candidate["authority_status"], "verified")
                else:
                    self.assertEqual(candidate["authority_status"], "verified")

    def test_registered_cases(self):
        path = Path(__file__).resolve().parents[1] / "diagnostics/heldout/stage36_6_cases.json"
        cases = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(len(cases["identity"]), 6)
        for case in cases["identity"]:
            with self.subTest(case=case):
                decision = assess_product_page_identity(
                    case["model"], f"<h1>{case['heading']}</h1>", case["url"],
                )
                self.assertEqual(decision.relation, case["expected"])
        for case in cases["authority"]:
            with self.subTest(case=case):
                trusted = TrustedSource(
                    "brand.example",
                    f'<a href="https://{case["domain"]}/">{case["anchor"]}</a>',
                    url="https://brand.example/partners",
                )
                decision = resolve_authority(
                    f"<p>{case['claim']}</p>", case["claim"], case["domain"],
                    case["brand"], (trusted,),
                )
                self.assertEqual(decision.role, case["expected"])


class BotNameOnlyScopeTests(unittest.IsolatedAsyncioTestCase):
    async def test_bot_uses_same_scoped_recovery_without_category_argument(self):
        rows = load_archive(Path("diagnostics/baselines/stage36_6/raw_sanitized.zip"))
        for index in (1, 16, 22, 37, 47):
            page_url, _ = CONFIRMED[index]
            row = rows[index - 1]
            match = next(item for item in row["secondary"] if _key(item["url"]) == _key(page_url))
            clear_official_domain_cache()
            service = DiscoveryDebugService(
                providers=lambda url=match["url"], title=match["title"]: [SavedProvider(url, title)],
                document_reader=lambda _url: None,
            )
            replies = []

            async def reply(message):
                replies.append(message)

            with self.subTest(index=index), patch("services.discovery_debug.fetch_working_page",
                                                 return_value=(page_url, PRIMARY[index])):
                result = await handle_discovery_query(row["input"], index, service, reply=reply)
                self.assertEqual(result.status, "PASS")
                self.assertTrue(replies)


if __name__ == "__main__":
    unittest.main()
