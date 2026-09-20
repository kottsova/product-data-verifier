"""Stage 36.6 gates shared by discovery, debug, extraction, and workflow."""

import unittest
import json
from datetime import date
from pathlib import Path

from core.authority import TrustedSource, resolve_authority
from core.authority_registry import find_seed
from core.discovery import DiscoveryOutcome, assess_candidate_relevance, rank_candidates
from core.identity import assess_product_page_identity
from core.workflow import ProductWorkflowRequest, run_product_workflow
from services.discovery_debug import DiscoverySource, _candidate_view, _result_from_outcome
from services.raw_extraction import RawExtractionService
from tests.test_workflow import FixtureServices, candidate


class AuthorityContractTests(unittest.TestCase):
    def test_seed_is_brand_host_and_time_scoped(self):
        self.assertIsNotNone(find_seed("Razer", "www.razer.com", today=date(2026, 9, 20)))
        self.assertIsNone(find_seed("Razer", "community.razer.com", today=date(2026, 9, 20)))
        self.assertIsNone(find_seed("Another", "razer.com", today=date(2026, 9, 20)))
        self.assertIsNone(find_seed("Razer", "razer.com", today=date(2028, 9, 20)))
        distributor = find_seed("TP-Link", "tp-link.cz", today=date(2026, 9, 20))
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
            "TP-Link", "Deco BE85",
        )[0]
        self.assertEqual(page["source_type"], "distributor")
        self.assertEqual(page["authority_status"], "verified")
        self.assertEqual(_candidate_view(page).group, "dealer")

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

    def test_content_can_prove_numeric_url(self):
        self.check_page("SN23EI03ME", "Siemens SN23EI03ME", "https://siemens-home.bsh-group.com/product/12345", "exact")

    def test_spaced_complete_sku_in_primary_heading_and_url(self):
        url = "https://braunhousehold.com/en/p/multiquick-9-hand-blender-mq-9187xli/HB901-MQ9187XLI.html"
        self.check_page("MultiQuick 9 MQ9187XLI", "MultiQuick 9 Hand blender MQ 9187XLI", url, "exact")
        self.check_page("MultiQuick 9 MQ9187XLI", "MultiQuick 9 Hand blender MQ 9187XLII", url, "unknown")

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
        items = rank_candidates([(url, "Razer DeathAdder V3")], "Razer", "DeathAdder V3")
        outcome = DiscoveryOutcome(items, "success", [], [], [])
        result = _result_from_outcome(
            "Razer DeathAdder V3", "Razer", "DeathAdder V3", "global", outcome, 0.1,
            fetch=lambda page_url: (page_url, "<html><title>App</title></html>"),
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


class HeldoutCases(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
