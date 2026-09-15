"""Deterministic tests for the Stage 17 benchmark harness (no live web)."""

from __future__ import annotations

import ast
import json
from pathlib import Path
import tempfile
import time
import unittest

from regression.runner import (
    aggregate_metrics,
    atomic_write_json,
    compare_references,
    load_dataset,
    result_record,
    resumable_products,
    run_products,
)
from services.product_verifier import (
    ServiceCategory,
    ServiceIdentity,
    ServiceQuality,
    VerifyProductRequest,
    VerifyProductResult,
)


ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / "regression" / "datasets" / "mvp_50_v1.json"
BASELINE = ROOT / "regression" / "baselines" / "reference_v1.json"


def _scheduler_worker(product, config, output_queue):
    """Spawn-safe worker used only to prove timeout isolation/continuation."""
    if product["id"] == "slow":
        time.sleep(2.0)
    output_queue.put((product["id"], {
        "product_id": product["id"], "final_success": True,
        "timed_out": False, "runtime_seconds": 0.001,
    }))


def _success_result(*, cached: bool = False) -> VerifyProductResult:
    request = VerifyProductRequest(brand="Acme", model="X100")
    return VerifyProductResult(
        success=True,
        request=request,
        identity=ServiceIdentity(
            brand="Acme", base_model="X100", commercial_model="X100",
            manufacturer_article=None, product_code=None, sku=None, gtin=None,
            color=None, configuration={}, confidence="high",
        ),
        category=ServiceCategory(
            category_id="cooktop", category_name="Cooktop",
            parent_category="major_appliance", confidence="high",
        ),
        quality=ServiceQuality(
            status="partial", coverage_percent=62.5, schema_total=8, schema_found=5,
            confirmed_count=4, unresolved_count=3, conflict_count=1,
            critical_total=3, critical_found=2, critical_confirmed=2,
            critical_conflict=1, critical_high_authority_confirmed=1,
            category_confidence="high", identity_confidence="high",
            reasons=("coverage is incomplete",), warnings=(),
        ),
        metadata={
            "initial_candidate_count": 7, "source_count": 3,
            "initial_fetch_count": 2, "targeted_fetch_count": 1,
            "discovery_status": "completed",
        },
        served_from_cache=cached,
    )


class DatasetTests(unittest.TestCase):
    def test_versioned_dataset_has_50_unique_real_product_identities(self):
        dataset = load_dataset(DATASET)
        products = dataset["products"]
        self.assertEqual(len(products), 50)
        self.assertEqual(len({item["id"] for item in products}), 50)
        self.assertTrue(all(item["brand"] and item["model"] for item in products))

    def test_dataset_is_balanced_across_all_supported_mvp_categories(self):
        dataset = load_dataset(DATASET)
        counts = {}
        for item in dataset["products"]:
            category = item["expected_category"]
            counts[category] = counts.get(category, 0) + 1
        self.assertEqual(set(counts), {
            "cooktop", "smartphone", "sewing_machine", "air_fryer", "wet_dry_vacuum",
        })
        self.assertEqual(set(counts.values()), {10})

    def test_all_five_continuity_products_have_reference_tag_and_baseline(self):
        dataset = load_dataset(DATASET)
        references = {item["id"] for item in dataset["products"] if "reference" in item["tags"]}
        baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
        baseline_ids = {item["product_id"] for item in baseline["products"]}
        self.assertEqual(len(references), 5)
        self.assertEqual(references, baseline_ids)

    def test_format_accepts_a_synthetic_300_item_scale_fixture(self):
        # This proves parser/data-structure readiness only; it is not presented
        # as a 300-product real-world dataset or live benchmark.
        template = load_dataset(DATASET)["products"][0]
        products = [{**template, "id": f"scale-{index}"} for index in range(300)]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "scale.json"
            path.write_text(json.dumps({
                "schema_version": "1.0", "dataset_id": "synthetic-scale-test",
                "products": products,
            }), encoding="utf-8")
            self.assertEqual(len(load_dataset(path)["products"]), 300)


class RecordAndAggregateTests(unittest.TestCase):
    def setUp(self):
        self.product = {
            "id": "acme-x100", "brand": "Acme", "model": "X100",
            "market": "DE", "expected_category": "cooktop",
        }

    def test_record_uses_only_stable_dto_and_marks_metric_availability(self):
        record = result_record(self.product, _success_result(), 1.2345678)
        self.assertEqual(record["coverage_percent"], 62.5)
        self.assertEqual(record["critical_missing_count"], 1)
        self.assertEqual(record["fetch_count"], 3)
        self.assertTrue(record["source_metric_availability"]["fetch_count"])
        self.assertEqual(record["runtime_seconds"], 1.234568)

    def test_missing_public_metadata_stays_explicitly_unavailable(self):
        result = _success_result()
        result = VerifyProductResult(success=True, request=result.request, quality=result.quality)
        record = result_record(self.product, result, 0.1)
        self.assertIsNone(record["source_count"])
        self.assertFalse(record["source_metric_availability"]["source_count"])

    def test_zero_candidate_partial_discovery_is_external_instability(self):
        result = _success_result()
        result = VerifyProductResult(
            success=True, request=result.request, quality=result.quality,
            metadata={"discovery_status": "partial", "initial_candidate_count": 0},
        )
        record = result_record(self.product, result, 0.1)
        self.assertTrue(record["blocking_marker"])

    def test_insufficient_is_success_and_is_grouped_as_low_evidence(self):
        base = result_record(self.product, _success_result(), 1.0)
        base["quality_status"] = "insufficient"
        aggregate = aggregate_metrics([base])
        self.assertEqual(aggregate["completed"], 1)
        self.assertEqual(aggregate["failed"], 0)
        self.assertEqual(aggregate["insufficient"], 1)
        self.assertEqual(aggregate["failure_groups"]["low_evidence_result"], 1)

    def test_reference_delta_does_not_automatically_mean_regression(self):
        record = result_record(self.product, _success_result(), 1.0)
        baseline = {"products": [{
            "product_id": "acme-x100", "expected_category": "cooktop",
            "stage9_coverage_percent": 65.0, "historical_coverage": "60-70%",
        }]}
        comparison = compare_references([record], baseline)[0]
        self.assertEqual(comparison["delta_percentage_points"], -2.5)
        self.assertEqual(comparison["assessment"], "no_deterministic_regression_signal")


class RepeatabilityAndSchedulingTests(unittest.TestCase):
    def test_atomic_json_artifact_is_readable(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "artifact.json"
            atomic_write_json(path, {"run_id": "abc", "records": [{"product_id": "p"}]})
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["run_id"], "abc")
            self.assertEqual(list(path.parent.glob("*.tmp")), [])

    def test_resume_skips_success_and_retries_failure_by_default(self):
        products = [{"id": "ok"}, {"id": "failed"}, {"id": "new"}]
        records = [
            {"product_id": "ok", "final_success": True},
            {"product_id": "failed", "final_success": False, "timed_out": True},
        ]
        pending = resumable_products(products, records, retry_failures=True)
        self.assertEqual([item["id"] for item in pending], ["failed", "new"])

    def test_hard_timeout_is_recorded_and_next_product_still_runs(self):
        products = [
            {"id": "slow", "brand": "Acme", "model": "Slow"},
            {"id": "fast", "brand": "Acme", "model": "Fast"},
        ]
        records = []
        run_products(
            products,
            # The deadline includes Windows spawn/import startup. Keep enough
            # headroom for the fast worker while remaining below the slow
            # worker's deliberate two-second sleep.
            {"concurrency": 1, "timeout_seconds": 1.5},
            records.append,
            worker_target=_scheduler_worker,
        )
        by_id = {item["product_id"]: item for item in records}
        self.assertTrue(by_id["slow"]["timed_out"])
        self.assertTrue(by_id["fast"]["final_success"])

    def test_harness_never_imports_internal_workflow(self):
        tree = ast.parse((ROOT / "regression" / "runner.py").read_text(encoding="utf-8"))
        imported = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                imported.append(node.module or "")
            elif isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)
        self.assertNotIn("core.workflow", imported)


if __name__ == "__main__":
    unittest.main()
