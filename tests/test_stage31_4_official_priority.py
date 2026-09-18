"""Stage 31.4 regression: verified official evidence outranks secondary sources.

Deterministic, no network. Pixel 9 Pro was the trigger (an exact official
Google page existed, but GSMArena became the displayed source); the tests use
a neutral "Acme X100" so no product or domain is hardcoded.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
import unittest

from core.discovery import (
    DiscoveryOutcome,
    ProviderAttempt,
    clear_official_domain_cache,
    discover_with_status,
)
from core.discovery import is_obvious_non_product_url, rank_candidates
from core.extract import RawAttribute
from core.match import candidate_model_match
from core.identity import resolve_product_identity
from core.official_source import (
    build_official_resolution,
    extract_auxiliary_links,
    extract_official_prose_facts,
    select_product_images,
)
from core.workflow import (
    ProductWorkflowRequest,
    WorkflowServices,
    run_product_workflow,
    select_source_candidates,
)
from bot.formatters import format_result, ordered_sources, product_image_urls
from bot.handlers import build_photos_callback, build_verify_command
from bot.jobs import JobManager
from tests.test_bot import (
    FakeCallbackQuery,
    FakeCallbackUpdate,
    FakeMessage,
    FakeUpdate,
    TrackingFakeService,
    _tap_language_button,
    make_result,
)
from services.product_verifier import VerifyProductRequest, _to_result
from tests.test_workflow import FixtureServices, candidate, fetch_result, raw


OFFICIAL = "https://acme.example/product/X100"
SECONDARY = "https://gsm.example/acme_x100.php"


def official(url=OFFICIAL, **kwargs):
    return candidate(url, title="Acme Smartphone X100", **kwargs)


def secondary(url=SECONDARY, **kwargs):
    kwargs.setdefault("source_type", "specialized_reference")
    kwargs.setdefault("authority", "unknown")
    kwargs.setdefault("relation", "same_base_model")
    kwargs.setdefault("score", 80)
    return candidate(url, title="Acme Smartphone X100", **kwargs)


def run(items, attributes, **fixture_kwargs):
    fixtures = FixtureServices(items, attributes, **fixture_kwargs)
    result = run_product_workflow(
        ProductWorkflowRequest(
            "Acme Smartphone X100", brand="Acme", targeted_search_enabled=False,
        ),
        services=fixtures.services(),
    )
    return result, fixtures


def by_name(result):
    return result.final_profile.by_name


class OfficialPriorityTests(unittest.TestCase):
    def test_exact_official_wins_source_when_secondary_also_present(self):
        result, _ = run(
            [secondary(), official()],
            {
                OFFICIAL: [raw("Processor", "Acme Chip 5", OFFICIAL)],
                SECONDARY: [raw(
                    "Chipset", "Acme Chip 5 (4 nm)", SECONDARY,
                    source_type="specialized_reference",
                )],
            },
        )
        processor = by_name(result)["processor"]
        self.assertEqual(processor.status, "Confirmed")
        self.assertEqual(processor.source, OFFICIAL)
        self.assertEqual(processor.authority_status, "verified")
        self.assertEqual(processor.supporting_sources[0].source, OFFICIAL)

    def test_secondary_fills_only_missing_canonical_field(self):
        result, _ = run(
            [official(), secondary()],
            {
                OFFICIAL: [raw("Processor", "Acme Chip 5", OFFICIAL)],
                SECONDARY: [
                    raw("Chipset", "Acme Chip 5", SECONDARY, source_type="specialized_reference"),
                    raw("Resolution", "1280 x 2856 pixels", SECONDARY,
                        source_type="specialized_reference"),
                ],
            },
        )
        attributes = by_name(result)
        self.assertEqual(attributes["processor"].source, OFFICIAL)
        self.assertEqual(attributes["display_resolution"].status, "Confirmed")
        self.assertEqual(attributes["display_resolution"].source, SECONDARY)

    def test_secondary_cannot_overwrite_valid_official_value(self):
        result, _ = run(
            [official(), secondary()],
            {
                OFFICIAL: [raw("Battery capacity", "5000 mAh", OFFICIAL)],
                SECONDARY: [raw(
                    "Battery capacity", "4500 mAh", SECONDARY,
                    source_type="specialized_reference",
                )],
            },
        )
        battery = by_name(result)["battery_capacity"]
        self.assertEqual(battery.status, "Confirmed")
        self.assertEqual(battery.value, "5000 mAh")
        self.assertEqual(battery.source, OFFICIAL)

    def test_conflict_preserves_both_evidence_official_authority_stays_higher(self):
        result, _ = run(
            [official(), secondary()],
            {
                OFFICIAL: [raw("Battery capacity", "5000 mAh", OFFICIAL)],
                SECONDARY: [raw(
                    "Battery capacity", "4500 mAh", SECONDARY,
                    source_type="specialized_reference",
                )],
            },
        )
        battery = by_name(result)["battery_capacity"]
        self.assertEqual(battery.resolution_reason, "higher_authority_source")
        self.assertEqual([e.source for e in battery.supporting_sources], [OFFICIAL])
        self.assertEqual([e.source for e in battery.conflicting_values], [SECONDARY])
        self.assertEqual(battery.conflicting_values[0].value, "4500 mAh")
        self.assertEqual(battery.supporting_sources[0].source_type, "manufacturer")
        self.assertEqual(battery.conflicting_values[0].source_type, "specialized_reference")

    def test_selection_prefers_official_exact_page_over_higher_scored_secondary(self):
        selected = select_source_candidates(
            [secondary(score=300), official(score=100)], 2,
        )
        self.assertEqual(selected[0]["url"], OFFICIAL)

    def test_inaccessible_official_falls_back_to_secondary_with_explained_gate(self):
        result, _ = run(
            [official(), secondary()],
            {SECONDARY: [raw(
                "Chipset", "Acme Chip 5", SECONDARY, source_type="specialized_reference",
            )]},
            failures={OFFICIAL},
        )
        self.assertEqual(by_name(result)["processor"].source, SECONDARY)
        gate = result.final_profile.metadata["official_source_resolution"]
        self.assertEqual(gate["official_domain_verified"], "yes")
        self.assertEqual(gate["official_exact_product_page_found"], "yes")
        self.assertEqual(gate["official_product_page_accessible"], "no")
        self.assertEqual(gate["official_failure_reason"], "fetch_failed")
        self.assertTrue(gate["official_fetch_status"].startswith("error:"))

    def test_gate_records_all_eight_fields_on_success(self):
        result, _ = run(
            [official(), secondary()],
            {OFFICIAL: [raw("Processor", "Acme Chip 5", OFFICIAL)]},
        )
        gate = result.final_profile.metadata["official_source_resolution"]
        self.assertEqual(gate["official_domain_candidate"], "acme.example")
        self.assertEqual(gate["official_domain_verified"], "yes")
        self.assertEqual(gate["official_domain_accessible"], "yes")
        self.assertEqual(gate["official_exact_product_page_found"], "yes")
        self.assertEqual(gate["official_product_page_accessible"], "yes")
        self.assertEqual(gate["official_fetch_status"], "success")
        self.assertGreaterEqual(gate["official_attributes_extracted_count"], 1)
        self.assertIsNone(gate["official_failure_reason"])

    def test_exact_model_mismatch_on_official_is_not_accepted(self):
        # The official product URL redirected to a category page that does not
        # name the model: its facts must not become exact-model official evidence.
        def redirected_fetch(item):
            source = fetch_result(item)
            if item["url"] == OFFICIAL:
                source["final_url"] = "https://acme.example/category/phones"
                source["html"] = (
                    "<html><head><title>Acme phones</title></head>"
                    "<body><h1>All phones</h1></body></html>"
                )
            return source

        fixtures = FixtureServices(
            [official(), secondary()],
            {
                OFFICIAL: [raw("Battery capacity", "9999 mAh", OFFICIAL)],
                SECONDARY: [raw(
                    "Battery capacity", "5000 mAh", SECONDARY,
                    source_type="specialized_reference",
                )],
            },
        )
        services = fixtures.services()
        services = WorkflowServices(
            services.discover_initial, services.discover_targeted,
            redirected_fetch, services.extract,
        )
        result = run_product_workflow(
            ProductWorkflowRequest(
                "Acme Smartphone X100", brand="Acme", targeted_search_enabled=False,
            ),
            services=services,
        )
        battery = by_name(result)["battery_capacity"]
        self.assertNotIn("9999", str(battery.value))
        self.assertNotIn(OFFICIAL, [e.source for e in battery.supporting_sources
                                    if e.authority_status == "verified"])
        gate = result.final_profile.metadata["official_source_resolution"]
        self.assertEqual(gate["official_product_page_accessible"], "no")
        self.assertEqual(gate["official_failure_reason"], "identity_rejected")

    def test_different_model_official_candidate_is_never_the_exact_page(self):
        other = candidate(
            "https://acme.example/product/X200", title="Acme X200",
            relation="different_model", relevance="reject",
        )
        gate = build_official_resolution(
            identity=resolve_product_identity("Acme Smartphone X100", brand="Acme"),
            candidates=[other],
        )
        self.assertEqual(gate["official_domain_verified"], "yes")
        self.assertEqual(gate["official_exact_product_page_found"], "no")
        self.assertEqual(gate["official_failure_reason"], "exact_model_not_found")

    def test_regional_official_domain_is_accepted(self):
        regional = candidate(
            "https://www.acme.co.uk/phones/x100", title="Acme Smartphone X100",
        )
        gate = build_official_resolution(
            identity=resolve_product_identity("Acme Smartphone X100", brand="Acme"),
            candidates=[regional],
            selected=[regional],
        )
        self.assertEqual(gate["official_domain_candidate"], "acme.co.uk")
        self.assertEqual(gate["official_domain_verified"], "yes")
        self.assertEqual(gate["official_exact_product_page_found"], "yes")

    def test_gate_failure_reasons_are_explicit(self):
        identity = resolve_product_identity("Acme Smartphone X100", brand="Acme")
        none = build_official_resolution(identity=identity, candidates=[secondary()])
        self.assertEqual(none["official_failure_reason"], "domain_not_found")
        self.assertEqual(none["official_domain_verified"], "no")

        unverified = candidate(
            "https://acme.example/product/X100", source_type="other", authority="unknown",
        )
        self.assertEqual(
            build_official_resolution(
                identity=identity, candidates=[unverified],
            )["official_failure_reason"],
            "domain_verification_failed",
        )

        no_model = candidate(
            "https://acme.example/", title="Acme", source_type="manufacturer",
        )
        no_model["model_match"] = "unknown"
        self.assertEqual(
            build_official_resolution(
                identity=identity, candidates=[no_model],
            )["official_failure_reason"],
            "exact_model_not_found",
        )

        page = official()
        blocked = fetch_result(page, status="error")
        blocked["blocked_reason"] = "captcha"
        gate = build_official_resolution(
            identity=identity, candidates=[page], selected=[page], fetched_sources=[blocked],
        )
        self.assertEqual(gate["official_failure_reason"], "inaccessible")

        ok = fetch_result(page)
        gate = build_official_resolution(
            identity=identity, candidates=[page], selected=[page], fetched_sources=[ok],
        )
        self.assertEqual(gate["official_failure_reason"], "extraction_failed")

        unmapped = RawAttribute(
            "Zzz", "1", None, OFFICIAL, "manufacturer", "e", "html_table", "high", "1", "product",
        )
        gate = build_official_resolution(
            identity=identity, candidates=[page], selected=[page], fetched_sources=[ok],
            raw_attributes=[unmapped],
        )
        self.assertEqual(gate["official_failure_reason"], "no_usable_canonical_attributes")


class OfficialDomainDiscoveryTests(unittest.TestCase):
    def test_official_domain_is_resolved_and_queried_before_ranking_secondary(self):
        clear_official_domain_cache()
        queries = []

        def searcher(query):
            queries.append(query)
            if "official" in query and "site:" not in query:
                return [("https://acme.example/", "Acme official website")]
            if "-site:" in query:
                return [("https://carrier.acme.example/phones/x100", "Acme X100 overview")]
            if "site:acme.example" in query:
                return [
                    ("https://store.acme.example/product/x100", "Acme X100"),
                    ("https://support.acme.example/answer/1", "Acme X100 help"),
                ]
            return [("https://gsm.example/acme_x100.php", "Acme X100 specs")]

        outcome = discover_with_status("Acme", "X100", searcher=searcher)
        urls = [item["url"] for item in outcome.candidates]

        official_position = next(
            i for i, q in enumerate(queries) if "site:acme.example" in q
        )
        self.assertTrue(any("official" in q for q in queries[:official_position]))
        # The sibling-host expansion excludes the hosts already seen and finds
        # the host the plain domain query missed; nothing about it is hardcoded.
        expansion = [q for q in queries if "-site:" in q]
        self.assertEqual(len(expansion), 1)
        self.assertIn("-site:store.acme.example", expansion[0])
        self.assertIn("-site:support.acme.example", expansion[0])
        self.assertIn("https://carrier.acme.example/phones/x100", urls)
        carrier = next(i for i in outcome.candidates if "carrier" in i["url"])
        self.assertEqual(carrier["authority_status"], "verified")
        self.assertEqual(carrier["source_type"], "manufacturer")


class OfficialPageDiscoveryRegressionTests(unittest.TestCase):
    """Why the exact official Pixel page never reached the pipeline."""

    def test_about_section_containing_the_model_is_not_a_non_product_url(self):
        url = "https://carrier.example/about/phones/acme-x100"
        self.assertTrue(is_obvious_non_product_url(url))                 # legacy behavior
        self.assertFalse(is_obvious_non_product_url(url, "Acme X100"))    # URL names the model
        self.assertTrue(is_obvious_non_product_url(url, "Acme X200"))     # a different model
        self.assertTrue(is_obvious_non_product_url(
            "https://acme.example/about/company", "Acme X100",
        ))
        # article-like sections are never exempt, even when they name the model
        self.assertTrue(is_obvious_non_product_url(
            "https://acme.example/blog/acme-x100", "Acme X100",
        ))

    def test_exact_official_family_page_is_not_vetoed_by_its_title(self):
        title = "Get the new Acme X100 and the Acme X100 Pro"
        url = "https://carrier.example/about/phones/acme-x100"
        self.assertEqual(candidate_model_match("Acme X100", title, url), "exact")
        # the same title on the sibling's own URL stays a different variant
        self.assertEqual(
            candidate_model_match(
                "Acme X100", title, "https://carrier.example/about/phones/acme-x100-pro",
            ),
            "different_variant",
        )
        # a title naming only the variant still vetoes an exact-slug URL
        self.assertEqual(
            candidate_model_match("Acme X100", "Acme X100 Pro", url), "different_variant",
        )

    def test_official_about_product_page_is_ranked_as_verified_exact_candidate(self):
        ranked = rank_candidates(
            [("https://carrier.acme.example/about/phones/acme-x100",
              "Get the new Acme X100 and the Acme X100 Pro | Carrier")],
            "Acme", "Acme X100",
            official_domains={"acme.example": "https://acme.example/"},
        )
        self.assertEqual(len(ranked), 1)
        self.assertEqual(ranked[0]["source_type"], "manufacturer")
        self.assertEqual(ranked[0]["authority_status"], "verified")
        self.assertEqual(ranked[0]["model_match"], "exact")


PROSE_HTML = """
<html><head><title>Acme X100 | Shop</title><script>var x = "9 GB RAM";</script></head><body>
<h1>Acme X100</h1>
<div>X100 6.3" display and X100 Max 6.8" display. The Super display runs up to 120 Hz.</div>
<div>The new Acme Chip 5 chip and 16 GB RAM power the Acme X100.</div>
<div>50 MP main camera. 48 MP telephoto. 48 MP ultrawide. 42 MP front camera.</div>
<div>50 MP main camera. 42 MP front camera.</div>
</body></html>
"""


def prose_source(html=PROSE_HTML, **overrides):
    item = official("https://acme.example/about/x100")
    source = fetch_result(item)
    source["html"] = html
    source.update(overrides)
    return source


class OfficialProseTests(unittest.TestCase):
    identity = resolve_product_identity("Acme X100", brand="Acme")

    def facts(self, source):
        return {
            item.name: item for item in
            extract_official_prose_facts(source, self.identity)
        }

    def test_extracts_canonical_facts_and_respects_sibling_variants(self):
        facts = self.facts(prose_source())
        self.assertEqual(facts["Display size"].value, "6.3")  # not the Max 6.8"
        self.assertEqual(facts["Display size"].unit, "in")
        self.assertEqual(facts["Refresh rate"].value, "120")
        self.assertEqual(facts["Processor"].value, "Acme Chip 5")
        self.assertEqual(facts["RAM"].value, "16")
        self.assertEqual(
            facts["Rear camera"].value, "50 MP main + 48 MP telephoto + 48 MP ultrawide",
        )
        self.assertEqual(facts["Front camera"].value, "42 MP")
        for fact in facts.values():
            self.assertEqual(fact.source_type, "manufacturer")

    def test_ambiguous_sibling_values_are_dropped_not_guessed(self):
        html = '<html><body><h1>Acme X100</h1>'\
               '<p>Acme Series 6.3" display. Acme Range 6.8" display.</p></body></html>'
        self.assertNotIn("Display size", self.facts(prose_source(html)))

    def test_prose_is_only_taken_from_verified_exact_official_pages(self):
        self.assertEqual(self.facts(prose_source(authority_status="unknown")), {})
        self.assertEqual(self.facts(prose_source(source_type="specialized_reference")), {})
        self.assertEqual(self.facts(prose_source(identity_relation="unknown")), {})
        self.assertEqual(self.facts(prose_source(model_relevance="unknown")), {})

    def test_prose_facts_map_to_confirmed_official_fields(self):
        source = prose_source()

        def extract(_source):
            return []

        item = official("https://acme.example/about/x100")
        fixtures = FixtureServices([item], {})
        services = fixtures.services()
        services = WorkflowServices(
            services.discover_initial, services.discover_targeted,
            lambda i: {**fetch_result(i), "html": source["html"]}, extract,
        )
        result = run_product_workflow(
            ProductWorkflowRequest(
                "Acme X100", brand="Acme", targeted_search_enabled=False,
            ),
            services=services,
        )
        attributes = by_name(result)
        for name in ("display_size", "refresh_rate", "processor", "rear_camera", "front_camera"):
            self.assertEqual(attributes[name].status, "Confirmed", name)
            self.assertEqual(attributes[name].source, item["url"], name)
            self.assertEqual(attributes[name].authority_status, "verified", name)


IMAGE_HTML = """
<html><head><meta property="og:image" content="https://cdn.acme.example/img/hero.jpg"></head><body>
<img src="https://cdn.acme.example/img/logo.png" alt="Acme logo">
<img src="https://cdn.acme.example/img/x100-front.jpg" alt="Front of the Acme X100">
<picture><source srcset="https://lh3.googleusercontent.com/abc=w300 300w, https://lh3.googleusercontent.com/abc=w900 900w">
<img alt="Back of the Acme X100"></picture>
<picture><source srcset="https://lh3.googleusercontent.com/abc=s4092-w4092-rw"><img alt=""></picture>
<img src="https://cdn.acme.example/icons/cart-icon.png" alt="cart">
<img src="https://cdn.acme.example/img/promo-banner.jpg" alt="Summer sale banner">
<img src="https://cdn.acme.example/img/vector.svg" alt="Acme X100">
<img src="http://cdn.acme.example/img/insecure.jpg" alt="Acme X100">
<img src="/media/side.jpg" alt="Side view of Acme X100">
</body></html>
"""


class ImageAndAuxiliaryTests(unittest.TestCase):
    def test_image_selection_prefers_official_cdn_images_and_filters_junk(self):
        page = prose_source(IMAGE_HTML)
        page["final_url"] = "https://acme.example/about/x100"
        gsm = fetch_result(secondary())
        gsm["html"] = '<img src="https://fdn.gsm.example/bigpic/phone.jpg" alt="Acme X100">'
        images = select_product_images([gsm, page], "X100")

        self.assertNotIn("https://fdn.gsm.example/bigpic/phone.jpg", images)  # secondary
        self.assertIn("https://cdn.acme.example/img/x100-front.jpg", images)
        self.assertIn("https://acme.example/media/side.jpg", images)
        self.assertIn("https://cdn.acme.example/img/hero.jpg", images)
        joined = " ".join(images)
        for junk in ("logo", "icon", "banner", ".svg", "insecure", "=w300"):
            self.assertNotIn(junk, joined)
        # sized variants of one picture collapse to a single (largest) URL
        self.assertEqual(sum("googleusercontent.com/abc" in url for url in images), 1)
        # ... and an oversized rendition is requested at the bounded size
        self.assertIn("https://lh3.googleusercontent.com/abc=s2048-w2048-rw", images)
        # images that name the model come first
        self.assertEqual(images[-1], "https://cdn.acme.example/img/hero.jpg")
        self.assertEqual(len(images), len(set(images)))

    def test_no_images_from_unverified_or_different_model_pages(self):
        self.assertEqual(
            select_product_images([prose_source(IMAGE_HTML, authority_status="unknown")], "X100"),
            [],
        )
        self.assertEqual(
            select_product_images(
                [prose_source(IMAGE_HTML, identity_relation="different_model")], "X100",
            ),
            [],
        )

    def test_auxiliary_links_are_separate_from_attributes(self):
        html = """
        <html><body>
        <a href="reviews.php3">Reviews</a>
        <a href="compare.php3">Compare</a>
        <a href="acme_x100-review-1.php">Review</a>
        <a href="acme_x100-pictures-1.php">Pictures</a>
        <a href="acme_x100-reviews-1.php">Opinions</a>
        <a href="compare.php3?id=1">Compare</a>
        <a href="acme_x100-price-1.php">Prices</a>
        <a href="related.php3?id=1">Related devices</a>
        <table><tr><td>Chipset</td><td>Acme Chip 5</td></tr></table>
        </body></html>"""
        item = secondary()
        source = fetch_result(item)
        source["html"] = html
        links = extract_auxiliary_links(source)
        self.assertEqual(
            sorted(link["kind"] for link in links),
            ["compare", "opinions", "pictures", "prices", "review"],
        )
        self.assertFalse(any("related" in link["url"] for link in links))
        by_kind = {link["kind"]: link["url"] for link in links}
        self.assertTrue(by_kind["review"].endswith("acme_x100-review-1.php"))  # not the site index
        self.assertTrue(by_kind["compare"].endswith("compare.php3?id=1"))     # product-scoped

        def extract(src):
            return [raw("Chipset", "Acme Chip 5", src["source_url"],
                        source_type="specialized_reference")]

        fixtures = FixtureServices([official(), item], {})
        services = fixtures.services()
        services = WorkflowServices(
            services.discover_initial, services.discover_targeted,
            lambda i: {**fetch_result(i), "html": html}, extract,
        )
        result = run_product_workflow(
            ProductWorkflowRequest(
                "Acme Smartphone X100", brand="Acme", targeted_search_enabled=False,
            ),
            services=services,
        )
        kinds = {link["kind"] for link in result.final_profile.metadata["auxiliary_links"]}
        self.assertEqual(kinds, {"compare", "opinions", "pictures", "prices", "review"})
        names = {a.canonical_name for a in result.final_profile.attributes}
        for label in ("review", "pictures", "opinions", "compare", "prices", "related_devices"):
            self.assertNotIn(label, names)


class PreviewSourceOrderTests(unittest.TestCase):
    def build(self):
        result, _ = run(
            [secondary(), official()],
            {
                OFFICIAL: [raw("Processor", "Acme Chip 5", OFFICIAL)],
                SECONDARY: [
                    raw("Chipset", "Acme Chip 5", SECONDARY, source_type="specialized_reference"),
                    raw("Resolution", "1280 x 2856 pixels", SECONDARY,
                        source_type="specialized_reference"),
                    raw("Bluetooth", "5.3", SECONDARY, source_type="specialized_reference"),
                ],
            },
        )
        return _to_result(VerifyProductRequest("Acme", "Smartphone X100"), result)

    def test_source_list_puts_official_first_even_when_secondary_backs_more(self):
        service_result = self.build()
        self.assertEqual(
            ordered_sources(service_result),
            [(OFFICIAL, True), (SECONDARY, False)],
        )

    def test_preview_lists_official_source_first(self):
        text = "\n".join(format_result(self.build(), language="en"))
        self.assertIn("Sources", text)
        block = text[text.index("🔗 Sources"):]
        self.assertLess(block.index(OFFICIAL), block.index(SECONDARY))
        self.assertIn("1. official source: " + OFFICIAL, block)
        self.assertIn("2. third-party source: " + SECONDARY, block)

    def test_preview_is_concise_attribute_equals_value_lines(self):
        lines = format_result(self.build(), language="en")[0].split("\n")
        self.assertTrue(any(line.startswith("Processor = ") for line in lines))
        # Per-attribute provenance lines are gone; sources are listed once.
        self.assertFalse(any(line.strip().startswith("\U0001f517") and ":" in line and "Processor" in line
                             for line in lines))

    def test_product_image_urls_only_returns_https(self):
        service_result = self.build()
        object.__setattr__(service_result, "metadata", {
            "product_images": ["https://a/1.jpg", "http://a/2.jpg", "https://a/1.jpg", 5],
        })
        self.assertEqual(product_image_urls(service_result), ["https://a/1.jpg"])


class PhotoMessage(FakeMessage):
    def __init__(self, *args, fail_photos=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.albums: list[list[str]] = []
        self._fail_photos = fail_photos

    async def reply_media_group(self, media):
        if self._fail_photos:
            raise ConnectionError("Telegram rejected the photos")
        self.albums.append([item.media for item in media])

    async def reply_photo(self, photo, caption=None):
        if self._fail_photos:
            raise ConnectionError("Telegram rejected the photos")
        self.albums.append([photo])


PHOTOS = [f"https://cdn.acme.example/img/{i}.jpg" for i in range(12)]


async def _finish_job(manager, message, result):
    verify = build_verify_command(manager)
    message.text = "Acme X100"
    await verify(FakeUpdate(message), None)
    await _tap_language_button(manager, message, "en")
    for _ in range(100):
        if any("Verifying" in text or "Sources" in text or "Confirmed" in text for text in message.sent):
            if manager.active_job_count() == 0:
                break
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.05)


def photo_buttons(message):
    return [
        button.callback_data
        for markup in message.reply_markups if markup is not None
        for row in markup.inline_keyboard for button in row
        if button.callback_data.startswith("photos:")
    ]


class PhotoButtonTests(unittest.IsolatedAsyncioTestCase):
    async def test_photo_button_only_when_official_images_exist(self):
        with_images = replace(make_result(), metadata={"product_images": PHOTOS})
        manager = JobManager(TrackingFakeService(with_images), max_concurrent_jobs=1)
        message = PhotoMessage()
        await _finish_job(manager, message, with_images)
        self.assertEqual(len(photo_buttons(message)), 1)

        without = make_result()
        manager = JobManager(TrackingFakeService(without), max_concurrent_jobs=1)
        message = PhotoMessage()
        await _finish_job(manager, message, without)
        self.assertEqual(photo_buttons(message), [])

    async def test_tap_sends_all_photos_in_albums_of_ten(self):
        with_images = replace(make_result(), metadata={"product_images": PHOTOS})
        manager = JobManager(TrackingFakeService(with_images), max_concurrent_jobs=1)
        message = PhotoMessage()
        await _finish_job(manager, message, with_images)
        data = photo_buttons(message)[0]

        query = FakeCallbackQuery(data, message)
        await build_photos_callback(manager)(FakeCallbackUpdate(query), None)

        self.assertTrue(query.answered)
        self.assertEqual([len(album) for album in message.albums], [10, 2])
        self.assertEqual([url for album in message.albums for url in album], PHOTOS)

    async def test_failed_photo_send_falls_back_to_links(self):
        with_images = replace(make_result(), metadata={"product_images": PHOTOS[:3]})
        manager = JobManager(TrackingFakeService(with_images), max_concurrent_jobs=1)
        message = PhotoMessage()
        await _finish_job(manager, message, with_images)
        data = photo_buttons(message)[0]

        failing = PhotoMessage(fail_photos=True)
        await build_photos_callback(manager)(
            FakeCallbackUpdate(FakeCallbackQuery(data, failing)), None,
        )
        self.assertEqual(failing.albums, [])
        self.assertIn(PHOTOS[0], failing.sent[-1])

    async def test_unknown_or_foreign_job_is_never_served(self):
        with_images = replace(make_result(), metadata={"product_images": PHOTOS[:2]})
        manager = JobManager(TrackingFakeService(with_images), max_concurrent_jobs=1)
        message = PhotoMessage(chat_id=42)
        await _finish_job(manager, message, with_images)
        data = photo_buttons(message)[0]

        stranger = PhotoMessage(chat_id=999)
        await build_photos_callback(manager)(
            FakeCallbackUpdate(FakeCallbackQuery(data, stranger)), None,
        )
        await build_photos_callback(manager)(
            FakeCallbackUpdate(FakeCallbackQuery("photos:missing", stranger)), None,
        )
        self.assertEqual(stranger.albums, [])
        self.assertEqual(len(stranger.sent), 2)


if __name__ == "__main__":
    unittest.main()
