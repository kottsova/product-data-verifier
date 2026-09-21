"""Stage 36.7: the hard limit must not change any authority/identity decision.

Fixed saved candidates (the Stage 36.6 accepted pages and the seven excluded
negatives) run through three routes and must agree on every trust field:

* inline  - the cooperative pipeline exactly as before this stage;
* worker  - the supervised child process that ``discover_name`` now uses;
* replay  - the checkpoints of the inline run replayed as after a hard stop.
"""

from __future__ import annotations

import csv
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from core.discovery import canonicalize_url, clear_official_domain_cache
from diagnostics.stage36_5_baseline import PRODUCTS, load_archive
from diagnostics.stage36_6_public_replay import ACCEPTED_PRIMARY, NEGATIVES, SavedProvider
from services.discovery_debug import DiscoveryDebugService
from services.discovery_isolation import ReplayState, Recorder, read_events

ROOT = Path(__file__).resolve().parents[1]
EXCLUDED = {17: "TP-Link distributor", 12: "Roborock accessory", 23: "Razer HyperSpeed",
            33: "Corsair SHIFT", 50: "CeraVe refill", 49: "Oral-B twin pack",
            19: "NETGEAR forum"}


def _cases() -> list[dict]:
    old = load_archive(ROOT / "diagnostics/baselines/stage36_5/raw.zip")
    cases = []
    with (ROOT / "diagnostics/baselines/stage36_6/manual_exact_audit.csv").open(
            newline="", encoding="utf-8") as handle:
        for record in csv.DictReader(handle):
            index = int(record["index"])
            brand, model, category, _ = PRODUCTS[index - 1]
            cases.append({"kind": "accepted", "index": index, "name": old[index - 1]["input"],
                          "url": record["product_url"], "title": f"{brand} {model}",
                          "html": ACCEPTED_PRIMARY[index]})
    for index, url, heading in NEGATIVES:
        cases.append({"kind": "excluded", "index": index, "name": old[index - 1]["input"],
                      "url": url, "title": heading, "html": f"<h1>{heading}</h1>"})
    return cases


def signature(result) -> dict:
    sources = [
        (group, canonicalize_url(item.url), item.authority_status, item.model_match,
         item.page_role, item.authority_evidence_kind)
        for group in ("official", "dealers", "secondary", "rejected")
        for item in getattr(result, group)
    ]
    return {
        "status": result.status,
        "exact_official_found": result.exact_official_found,
        "sources": sorted(sources),
        "official_pages": sorted(canonicalize_url(item.url) for item in result.official_pages),
        "support_pages": sorted(canonicalize_url(item.url) for item in result.support_pages),
        "documents": sorted(canonicalize_url(item.url) for item in result.documents),
        "fetch_relations": sorted(
            (canonicalize_url(item["url"]), item.get("main_product_relation", ""))
            for item in result.page_fetches
        ),
    }


def _inline(case: dict, recorder=None):
    clear_official_domain_cache()
    provider = SavedProvider(case["url"], case["title"])
    service = DiscoveryDebugService(
        providers=lambda: [provider], document_reader=lambda _url: None, isolation="inline",
    )
    with patch("services.discovery_debug.fetch_working_page",
               return_value=(case["url"], case["html"])):
        return service._discover_inline(case["name"], recorder=recorder)


class DecisionEquivalenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.cases = _cases()

    def test_case_set_is_the_ten_accepted_and_seven_excluded(self) -> None:
        self.assertEqual(sum(c["kind"] == "accepted" for c in self.cases), 10)
        self.assertEqual({c["index"] for c in self.cases if c["kind"] == "excluded"}, set(EXCLUDED))

    def test_excluded_items_stay_excluded_in_every_route(self) -> None:
        for case in (c for c in self.cases if c["kind"] == "excluded"):
            with self.subTest(case=EXCLUDED[case["index"]]):
                result = _inline(case)
                self.assertFalse(result.exact_official_found)
                self.assertFalse(any(
                    page.model_match == "exact" for page in result.official_pages
                ))

    def test_worker_route_equals_inline_route(self) -> None:
        for case in self.cases:
            with self.subTest(index=case["index"], kind=case["kind"]):
                expected = signature(_inline(case))
                os.environ["PDV_TEST_CASE"] = json.dumps(case)
                try:
                    isolated = DiscoveryDebugService(
                        wall_clock_budget_seconds=30.0,
                        worker_hooks="tests.isolation_hooks:saved_case",
                    ).discover_name(case["name"])
                finally:
                    os.environ.pop("PDV_TEST_CASE", None)
                self.assertTrue(isolated.performance["isolation"]["result_complete"])
                self.assertEqual(signature(isolated), expected)

    def test_checkpoint_replay_equals_the_run_it_recorded(self) -> None:
        for case in self.cases:
            with self.subTest(index=case["index"], kind=case["kind"]):
                with tempfile.TemporaryDirectory() as directory:
                    recorder = Recorder(Path(directory))
                    live = _inline(case, recorder=recorder)
                    state = ReplayState()
                    for event in read_events(Path(directory) / "events.log"):
                        state.apply(event)
                    replayed = DiscoveryDebugService(isolation="inline")._discover_inline(
                        case["name"], replay=state,
                    )
                    recorder._file.close()
                self.assertEqual(signature(replayed), signature(live))

    def test_truncated_checkpoints_never_grant_trust(self) -> None:
        """A run cut before the page fetch may lose evidence, never gain trust."""
        for case in (c for c in self.cases if c["kind"] == "excluded"):
            with self.subTest(case=EXCLUDED[case["index"]]):
                with tempfile.TemporaryDirectory() as directory:
                    recorder = Recorder(Path(directory))
                    _inline(case, recorder=recorder)
                    recorder._file.close()
                    events = [e for e in read_events(Path(directory) / "events.log")
                              if e["e"] != "fetch_done"]
                state = ReplayState()
                for event in events:
                    state.apply(event)
                replayed = DiscoveryDebugService(isolation="inline")._discover_inline(
                    case["name"], replay=state,
                )
                self.assertFalse(replayed.exact_official_found)


if __name__ == "__main__":
    unittest.main()
