"""Stage 33.0: name-only, traceable, discovery-only source finding."""

from __future__ import annotations

import unittest

from bot.handlers import handle_discovery_query
from bot.telegram_bot import build_application
from config import AppConfig
from core.authority import TrustedSource, resolve_authority
from core.discovery import (
    ResilientSearchSession,
    SearchResultRecord,
    canonicalize_url,
    classify_source,
    clear_official_domain_cache,
    discover_with_status,
)
from core.match import candidate_model_match
from services.discovery_debug import (
    DiscoveryDebugResult,
    DiscoverySource,
    parse_discovery_product_name,
)


class IdentityAndUrlTests(unittest.TestCase):
    def test_exact_model_and_variant_rejection(self):
        self.assertEqual(candidate_model_match(
            "Pixel 9", "Google Pixel 9", "https://store.example/pixel-9",
        ), "exact")
        self.assertEqual(candidate_model_match(
            "Pixel 9", "Google Pixel 9 Pro", "https://store.example/pixel-9-pro",
        ), "different_variant")
        self.assertEqual(candidate_model_match(
            "Pixel 9 Pro", "Google Pixel 9 Pro XL", "https://store.example/pixel-9-pro-xl",
        ), "different_variant")
        self.assertEqual(candidate_model_match(
            "Pixel 9 Pro", "Google Pixel 9", "https://store.example/pixel-9",
        ), "different_variant")
        self.assertEqual(candidate_model_match(
            "Pixel 9", "Google Pixel 9", "https://store.example/product/pixel_9a",
        ), "different_variant")
        self.assertEqual(candidate_model_match(
            "Pixel 9", "Google Pixel 9", "https://store.example/product/pixel9a",
        ), "different_variant")
        self.assertEqual(candidate_model_match(
            "Pixel 9 Pro", "Google Pixel 9 Pro Fold", "https://store.example/product/pixel_9_pro_fold",
        ), "different_variant")
        self.assertEqual(candidate_model_match(
            "Pixel 9 Pro", "Google Pixel 9 Pro", "https://store.example/docs/pixel9pro_pixel9proxl.pdf",
        ), "different_variant")
        self.assertEqual(candidate_model_match(
            "Sonicare 9900 Prestige HX9992/12", "Philips HX9992/12", "https://philips.example/c-p/HX9992_21/sonicare-9900-prestige",
        ), "different_variant")
        self.assertEqual(candidate_model_match(
            "Sonicare 9900 Prestige HX9992/12", "Philips Sonicare 9900 Prestige",
            "https://philips.example/c-p/HX9992_12/sonicare-9900-prestige",
        ), "exact")
        self.assertEqual(candidate_model_match(
            "MX Master 3S", "Logitech MX Master 3S for Mac",
            "https://logitech.example/shop/p/mx-master-3s-mac-mouse",
        ), "different_variant")
        self.assertEqual(candidate_model_match(
            "V15 Detect", "Dyson V15 Detect Pro specifications",
            "https://dyson.example/content/Dyson%20V15%20Detect%20Pro%20specs.pdf",
        ), "different_variant")

    def test_tracking_cleanup_preserves_product_page(self):
        self.assertEqual(
            canonicalize_url(
                "https://fi.google.com/about/phones/pixel-9-pro/?srsltid=abc"
                "&utm_campaign=x&gad_source=1#specs"
            ),
            "https://fi.google.com/about/phones/pixel-9-pro",
        )

    def test_multiword_brand_name_parsing_is_generic(self):
        self.assertEqual(
            parse_discovery_product_name("The Ordinary Niacinamide 10% + Zinc 1%"),
            ("The Ordinary", "Niacinamide 10% + Zinc 1%"),
        )
        self.assertEqual(
            parse_discovery_product_name("Fisher & Paykel CI604DTB4"),
            ("Fisher & Paykel", "CI604DTB4"),
        )


class ClassificationTests(unittest.TestCase):
    def setUp(self):
        clear_official_domain_cache()

    @staticmethod
    def searcher(query):
        if query == "Acme official website":
            return [("https://acme.co.uk/", "Acme Official Website")]
        return [
            SearchResultRecord(
                "https://acme.co.uk/products/x100?utm_source=search",
                "Acme X100 Product", provider="fake", query=query,
            ),
            SearchResultRecord(
                "https://support.acme.co.uk/manuals/x100.pdf?srsltid=1",
                "Acme X100 Manual", provider="fake", query=query,
            ),
        ]

    def test_manufacturer_regional_domain_and_support_page(self):
        outcome = discover_with_status("Acme", "X100", searcher=self.searcher)
        by_url = {item["url"]: item for item in outcome.candidates}
        product = by_url["https://acme.co.uk/products/x100"]
        manual = by_url["https://support.acme.co.uk/manuals/x100.pdf"]
        self.assertEqual((product["source_type"], product["authority_status"]),
                         ("manufacturer", "verified"))
        self.assertEqual((manual["source_type"], manual["authority_status"]),
                         ("official_document", "verified"))

    def test_authorized_dealer_requires_independent_corroboration(self):
        dealer = '<p>Acme authorized dealer</p><meta property="og:site_name" content="Acme">'
        trusted = [TrustedSource(
            "acme.com", '<a href="https://acme-shop.example/dealers">Dealer</a>',
        )]
        assessment = resolve_authority(
            dealer, "Acme authorized dealer", "acme-shop.example", "Acme", trusted,
        )
        self.assertEqual(assessment.role, "authorized_dealer")

    def test_secondary_classification_is_separate(self):
        self.assertEqual(classify_source("gsmarena.com", None), "specialized_reference")
        self.assertNotEqual(classify_source("amazon.com", None), "manufacturer")

    def test_search_pages_cannot_prove_model_and_user_uploads_are_not_official(self):
        def searcher(query):
            if query == "Bosch official website":
                return [("https://bosch.com/", "Bosch official website")]
            return [
                ("https://bosch.com/de/suche?q=PUE611BB5E", "Bosch PUE611BB5E"),
                ("https://drive.google.com/file/d/123", "Bosch PUE611BB5E"),
                ("https://bosch.com/products/pue611bb5e", "Bosch PUE611BB5E"),
            ]

        outcome = discover_with_status("Bosch", "PUE611BB5E", searcher=searcher)
        urls = {candidate["url"] for candidate in outcome.candidates}
        self.assertEqual(urls, {"https://bosch.com/products/pue611bb5e"})
        rejected = {item["url"] for item in outcome.rejected_candidates}
        self.assertIn("https://bosch.com/de/suche?q=PUE611BB5E", rejected)

    def test_matching_regional_product_ecosystem_verifies_brand_extension(self):
        def searcher(query):
            if query == "Acme official website":
                return []
            return [
                ("https://acme-home.co.uk/en/product/x100", "Acme X100 product"),
                ("https://acme-home.com.au/en/product/x100", "Acme X100 product"),
                ("https://acme-shop.co.uk/products/x100", "Acme X100 shop"),
                ("https://acme-shop.com.au/products/x100", "Acme X100 shop"),
            ]

        result = discover_with_status("Acme", "X100", searcher=searcher)
        by_url = {candidate["url"]: candidate for candidate in result.candidates}
        self.assertEqual(by_url["https://acme-home.co.uk/en/product/x100"]["authority_status"], "verified")
        self.assertNotEqual(by_url["https://acme-shop.co.uk/products/x100"]["authority_status"], "verified")

    def test_regional_sku_is_official_but_not_an_exact_model(self):
        def searcher(query):
            if query == "Acme official website":
                return []
            return [("https://acme.co.uk/en-gb/product/x100-gb/drill", "Acme X100-GB drill")]

        result = discover_with_status("Acme", "X100", searcher=searcher)
        item = result.candidates[0]
        self.assertEqual(item["authority_status"], "verified")
        self.assertEqual(item["relevance_relation"], "likely_variant")

    def test_family_page_title_alone_is_not_exact(self):
        def searcher(query):
            if query == "Acme official website":
                return [("https://acme.com/", "Acme official website")]
            return [("https://acme.com/vacuums/v15", "Acme V15 Detect")]

        result = discover_with_status("Acme", "V15 Detect", searcher=searcher)
        self.assertEqual(result.candidates[0]["relevance_relation"], "weak")

    def test_matching_family_name_does_not_override_wrong_sku_in_url(self):
        def searcher(query):
            if query == "Acme official website":
                return [("https://acme.com/", "Acme official website")]
            return [("https://acme.com/c-p/HX9994_12/sonicare-9900-prestige",
                     "Acme Sonicare 9900 Prestige")]

        result = discover_with_status("Acme", "Sonicare 9900 Prestige HX9992/12", searcher=searcher)
        self.assertFalse(result.candidates)
        self.assertTrue(any("conflicting model identifier" in item["relevance_reasons"][0]
                            for item in result.rejected_candidates))


class TraceAndAggregationTests(unittest.TestCase):
    def setUp(self):
        clear_official_domain_cache()

    def test_duplicate_merge_and_no_silent_drop(self):
        def searcher(query):
            if query == "Acme official website":
                return [("https://acme.com/", "Acme Official Website")]
            return [
                ("https://acme.com/product/x100?utm_source=a", "Acme X100"),
                ("https://acme.com/product/x100?srsltid=b", "Acme X100 duplicate"),
                ("https://acme.com/about", "About Acme"),
                ("javascript:void(0)", "Broken"),
            ]

        outcome = discover_with_status("Acme", "X100", searcher=searcher)
        trace = outcome.trace
        self.assertGreater(trace.collected_result_count, 0)
        self.assertGreater(trace.duplicate_count, 0)
        self.assertTrue(any(item.reason == "invalid_or_non_http_url" for item in trace.entries))
        self.assertTrue(any(item.reason == "obvious_non_product_or_blocked_url" for item in trace.entries))
        self.assertFalse(any(item.reason == "unclassified_pre_ranking_drop" for item in trace.entries))
        self.assertEqual(
            sum(item.outcome in {"accepted", "rejected", "merged_duplicate"} for item in trace.entries),
            trace.collected_result_count,
        )

    def test_multiple_queries_are_aggregated(self):
        def searcher(query):
            if query == "Acme official website":
                return [("https://acme.com/", "Acme Official Website")]
            if query.endswith(" specs"):
                return [("https://acme.com/specs/x100", "Acme X100 Specs")]
            if query == "Acme X100":
                return [("https://acme.com/product/x100", "Acme X100")]
            return []

        outcome = discover_with_status("Acme", "X100", searcher=searcher)
        urls = {item["url"] for item in outcome.candidates}
        self.assertIn("https://acme.com/product/x100", urls)
        self.assertIn("https://acme.com/specs/x100", urls)
        self.assertTrue(any(query.endswith(" specs") for query in outcome.attempted_queries))
        self.assertTrue(any(query.endswith(" official") for query in outcome.attempted_queries))

    def test_provider_falls_through_when_results_are_only_wrong_variants(self):
        class Provider:
            def __init__(self, name, results):
                self.name, self.results, self.calls = name, results, 0

            def search(self, query):
                self.calls += 1
                return self.results

        wrong = Provider("wrong", [
            ("https://acme.example/product/pixel-9-pro-fold", "Acme Pixel 9 Pro Fold"),
        ])
        right = Provider("right", [
            ("https://acme.example/product/pixel-9-pro", "Acme Pixel 9 Pro"),
        ])
        session = ResilientSearchSession(providers=(wrong, right))
        session.configure_identity("Acme", "Pixel 9 Pro")
        outcome = session.search_with_status("Acme Pixel 9 Pro")
        self.assertEqual([attempt.provider for attempt in outcome.attempts], ["wrong", "right"])
        self.assertEqual(right.calls, 1)


class BotDiscoveryFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_bot_flow_returns_links_without_verification(self):
        source = DiscoverySource(
            "https://acme.com/product/x100", "acme.com", "manufacturer",
            "Acme X100", "exact", "manufacturer", "exact model", "official",
        )
        result = DiscoveryDebugResult(
            "Acme X100", "Acme", "X100", "global", "PASS", True,
            (source,), (), (), (), 0.1, "success", ("Acme X100",), ("fake",), (),
            {"provider_raw": 1, "collected": 1, "normalized": 1, "unique": 1,
             "duplicates": 0, "accepted": 1, "rejected": 0, "official": 1,
             "dealer": 0, "secondary": 0},
        )

        class FakeService:
            def discover_name(self, name, *, chat_id=None):
                self.called = (name, chat_id)
                return result

            def last_result(self, chat_id):
                return result

        service = FakeService()
        replies = []

        async def reply(text):
            replies.append(text)

        returned = await handle_discovery_query(
            "Acme X100", 7, service, reply=reply,
        )
        self.assertIs(returned, result)
        self.assertEqual(service.called, ("Acme X100", 7))
        self.assertIn("https://acme.com/product/x100", "\n".join(replies))
        self.assertNotIn("характеристик", "\n".join(replies).casefold())

    async def test_temporary_plain_text_mode_routes_only_to_discovery(self):
        from unittest.mock import patch
        from telegram.ext import CommandHandler, MessageHandler

        with patch("bot.telegram_bot.Application") as application_class:
            app = application_class.builder.return_value.token.return_value.post_init.return_value.post_shutdown.return_value.build.return_value
            build_application("test-token", object(), object(), discovery_only=True)
        text_handlers = [handler for group in app.add_handler.call_args_list
                         for handler in group.args if isinstance(handler, MessageHandler)]
        self.assertEqual(len(text_handlers), 1)
        self.assertEqual(text_handlers[0].callback.__name__, "discovery_text")
        command_names = [name for call in app.add_handler.call_args_list
                         for handler in call.args if isinstance(handler, CommandHandler)
                         for name in handler.commands]
        self.assertEqual(set(command_names), {"start", "help", "discover", "discover_rejected"})

    def test_discovery_only_flag_is_opt_in(self):
        self.assertFalse(AppConfig.from_env({}).discovery_only)
        self.assertTrue(AppConfig.from_env({"PRODUCT_VERIFIER_DISCOVERY_ONLY": "true"}).discovery_only)

    def test_debug_bot_startup_skips_verification_service_and_database(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from bot import telegram_bot

        with (patch.object(telegram_bot.AppConfig, "from_env", return_value=AppConfig(discovery_only=True)),
              patch.object(telegram_bot.TelegramConfig, "from_env", return_value=SimpleNamespace(bot_token="test-token")),
              patch.object(telegram_bot, "configure_logging"),
              patch.object(telegram_bot, "build_product_verifier_service") as build_verification,
              patch.object(telegram_bot, "validate_runtime_wiring") as validate_database,
              patch.object(telegram_bot, "build_application") as build_bot):
            telegram_bot.main()
        build_verification.assert_not_called()
        validate_database.assert_not_called()
        self.assertTrue(build_bot.call_args.kwargs["discovery_only"])
        self.assertIsNone(build_bot.call_args.args[1])


if __name__ == "__main__":
    unittest.main()
