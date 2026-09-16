"""Deterministic contract tests for the read-only Stage 18.1 diagnostic."""

from __future__ import annotations

import json
from pathlib import Path
import unittest

from core.workflow import ProductWorkflowRequest, run_product_workflow
from diagnostics.live_quality_diagnosis import PRODUCTS, build_stage_trace
from tests.test_workflow import FixtureServices, candidate, raw


ROOT = Path(__file__).resolve().parents[1]


class LiveQualityDiagnosticTests(unittest.TestCase):
    def test_reference_requests_exactly_match_stage17_dataset(self):
        dataset = json.loads(
            (ROOT / "regression" / "datasets" / "mvp_50_v1.json").read_text(encoding="utf-8")
        )
        references = {
            item["id"]: item for item in dataset["products"] if "reference" in item["tags"]
        }
        self.assertEqual(set(references), {item["id"] for item in PRODUCTS.values()})
        for product in PRODUCTS.values():
            expected = references[product["id"]]
            for field in ("brand", "model", "article", "market", "expected_category"):
                self.assertEqual(product.get(field), expected.get(field))

    def test_trace_exposes_every_pipeline_stage_without_recomputing_it(self):
        url = "https://acme.example/product/X100"
        fixture = FixtureServices(
            [candidate(url, title="Acme Smartphone X100")],
            {url: [
                raw("Brand", "Acme", url),
                raw("Model", "X100", url),
                raw("Battery capacity", "5000 mAh", url),
            ]},
        )
        result = run_product_workflow(
            ProductWorkflowRequest(
                "Acme Smartphone X100", brand="Acme", targeted_search_enabled=False,
            ),
            services=fixture.services(),
        )
        product = {
            "id": "fixture", "brand": "Acme", "model": "X100", "article": None,
            "market": "global", "expected_category": "smartphone",
        }
        trace = build_stage_trace(product, result)
        self.assertEqual(set(trace), {
            "input", "identity", "discovery", "relevance_and_ranking", "fetch",
            "extraction", "category_and_schema", "mapping", "gap_detection",
            "targeted_search", "validation", "quality", "coverage_audit",
            "budget",
        })
        self.assertEqual(trace["fetch"]["attempted"], 1)
        self.assertEqual(trace["extraction"]["total"], 3)
        self.assertEqual(trace["mapping"]["mapped_count"], 3)
        self.assertEqual(trace["category_and_schema"]["category"], "smartphone")
        self.assertFalse(trace["targeted_search"]["enabled"])


if __name__ == "__main__":
    unittest.main()
