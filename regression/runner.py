"""Run bounded, resumable Product Data Verifier regression benchmarks.

Each product runs in its own process.  That makes the per-product timeout a
real hard boundary: a stuck browser/network stack can be terminated without
stopping the rest of the dataset.  The child process calls only the public
``ProductVerifierService.verify`` API.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import multiprocessing
from pathlib import Path
import queue
import statistics
import time
from typing import Any, Callable, Iterable, Mapping, Sequence
import uuid

from services.cache import CachePolicy, SqliteProductVerificationRepository
from services.product_verifier import ProductVerifierService, VerifyProductRequest, VerifyProductResult


SCHEMA_VERSION = "1.0"
SUPPORTED_CATEGORIES = {
    "cooktop", "smartphone", "laptop", "sewing_machine", "air_fryer", "wet_dry_vacuum",
}
SUPPORTED_MARKETS = {"DE", "GB", "RU", "US", "global"}
QUALITY_STATUSES = {"verified", "partial", "insufficient", "conflicted"}
EXTERNAL_MARKERS = (
    "blocked", "blocking", "captcha", "robots", "rate limit", "403", "429",
    "timeout", "timed out", "network", "connection", "dns", "unreachable",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def load_dataset(path: str | Path) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"dataset schema_version must be {SCHEMA_VERSION!r}")
    products = data.get("products")
    if not isinstance(products, list) or not products:
        raise ValueError("dataset products must be a non-empty list")
    seen: set[str] = set()
    for index, product in enumerate(products):
        if not isinstance(product, dict):
            raise ValueError(f"products[{index}] must be an object")
        for field in ("id", "brand", "model"):
            if not isinstance(product.get(field), str) or not product[field].strip():
                raise ValueError(f"products[{index}].{field} must be a non-empty string")
        if product["id"] in seen:
            raise ValueError(f"duplicate product id: {product['id']}")
        seen.add(product["id"])
        category = product.get("expected_category")
        if category is not None and category not in SUPPORTED_CATEGORIES:
            raise ValueError(f"unsupported expected_category for {product['id']}: {category}")
        market = product.get("market", "global")
        if market not in SUPPORTED_MARKETS:
            raise ValueError(f"unsupported market for {product['id']}: {market}")
        tags = product.get("tags", [])
        if not isinstance(tags, list) or any(not isinstance(tag, str) for tag in tags):
            raise ValueError(f"tags for {product['id']} must be a list of strings")
    return data


def _number(metadata: Mapping[str, object], key: str) -> int | None:
    value = metadata.get(key)
    if isinstance(value, bool):
        return None
    return int(value) if isinstance(value, (int, float)) else None


def _contains_external_marker(values: Iterable[object]) -> bool:
    text = " ".join(str(value).lower() for value in values if value is not None)
    return any(marker in text for marker in EXTERNAL_MARKERS)


def _record_is_external(record: Mapping[str, Any]) -> bool:
    discovery_status = record.get("discovery_status")
    return bool(
        record.get("blocking_marker")
        or record.get("timed_out")
        or discovery_status in {"blocked", "error"}
        or (
            discovery_status == "partial"
            and record.get("sources_discovered") == 0
        )
    )


def result_record(
    product: Mapping[str, Any], result: VerifyProductResult, runtime_seconds: float,
) -> dict[str, Any]:
    """Flatten the stable service DTO into a compact benchmark record."""
    quality = result.quality
    metadata = dict(result.metadata)
    initial_fetch = _number(metadata, "initial_fetch_count")
    targeted_fetch = _number(metadata, "targeted_fetch_count")
    fetch_count = (
        initial_fetch + targeted_fetch
        if initial_fetch is not None and targeted_fetch is not None else None
    )
    error_kind = result.error.kind if result.error else None
    error_message = result.error.message if result.error else None
    reasons = list(quality.reasons) if quality else []
    warnings = list(quality.warnings) if quality else []
    discovery_status = metadata.get("discovery_status")
    discovered_count = _number(metadata, "initial_candidate_count")
    discovery_external = (
        discovery_status in {"blocked", "error"}
        or (discovery_status == "partial" and discovered_count == 0)
    )
    external = error_kind == "workflow_failure" or _contains_external_marker(
        [error_message, discovery_status, *reasons, *warnings]
    ) or discovery_external
    critical_missing = None
    if quality is not None:
        critical_missing = max(quality.critical_total - quality.critical_found, 0)
    source_metrics = {
        "sources_discovered": discovered_count,
        "source_count": _number(metadata, "source_count"),
        "initial_fetch_count": initial_fetch,
        "targeted_fetch_count": targeted_fetch,
        "fetch_count": fetch_count,
    }
    return {
        "product_id": product["id"],
        "brand": product["brand"],
        "model": product["model"],
        "article": product.get("article"),
        "market": product.get("market", "global"),
        "expected_category": product.get("expected_category"),
        "category": result.category.category_id if result.category else None,
        "category_confidence": result.category.confidence if result.category else None,
        "identity_confidence": result.identity.confidence if result.identity else None,
        "coverage_percent": quality.coverage_percent if quality else None,
        "quality_status": quality.status if quality else None,
        "confirmed_count": quality.confirmed_count if quality else None,
        "unresolved_count": quality.unresolved_count if quality else None,
        "conflict_count": quality.conflict_count if quality else None,
        "critical_missing_count": critical_missing,
        "critical_conflict_count": quality.critical_conflict if quality else None,
        "served_from_cache": result.served_from_cache,
        "cache_state": "hit" if result.served_from_cache else "miss",
        **source_metrics,
        "source_metric_availability": {
            key: value is not None for key, value in source_metrics.items()
        },
        "runtime_seconds": round(runtime_seconds, 6),
        "final_success": result.success,
        "error_kind": error_kind,
        "error_message": error_message,
        "timed_out": False,
        "blocking_marker": external,
        "discovery_status": discovery_status,
        "quality_reasons": reasons,
        "quality_warnings": warnings,
    }


def timeout_record(product: Mapping[str, Any], timeout_seconds: float) -> dict[str, Any]:
    return _failure_record(
        product,
        runtime_seconds=timeout_seconds,
        error_kind="timeout",
        message=f"hard per-product timeout after {timeout_seconds:g}s",
        timed_out=True,
        external=True,
    )


def _failure_record(
    product: Mapping[str, Any], *, runtime_seconds: float, error_kind: str,
    message: str, timed_out: bool = False, external: bool = False,
) -> dict[str, Any]:
    return {
        "product_id": product["id"], "brand": product["brand"],
        "model": product["model"], "article": product.get("article"),
        "market": product.get("market", "global"),
        "expected_category": product.get("expected_category"),
        "category": None, "category_confidence": None, "identity_confidence": None,
        "coverage_percent": None, "quality_status": None, "confirmed_count": None,
        "unresolved_count": None, "conflict_count": None,
        "critical_missing_count": None, "critical_conflict_count": None,
        "served_from_cache": False, "cache_state": "miss",
        "sources_discovered": None, "source_count": None,
        "initial_fetch_count": None, "targeted_fetch_count": None, "fetch_count": None,
        "source_metric_availability": {
            "sources_discovered": False, "source_count": False,
            "initial_fetch_count": False, "targeted_fetch_count": False,
            "fetch_count": False,
        },
        "runtime_seconds": round(runtime_seconds, 6), "final_success": False,
        "error_kind": error_kind, "error_message": message, "timed_out": timed_out,
        "blocking_marker": external, "discovery_status": None,
        "quality_reasons": [], "quality_warnings": [],
    }


def _worker(product: dict[str, Any], config: dict[str, Any], output_queue: Any) -> None:
    started = time.monotonic()
    try:
        repository = SqliteProductVerificationRepository(config["cache_db"])
        service = ProductVerifierService(
            repository=repository,
            cache_policy=CachePolicy(ttl_seconds=config["cache_ttl_seconds"]),
        )
        request = VerifyProductRequest(
            brand=product["brand"], model=product["model"],
            article=product.get("article"), market=product.get("market", "global"),
            max_sources=config["max_sources"],
            targeted_search_enabled=config["targeted_search_enabled"],
            force_refresh=config["mode"] == "cold",
        )
        result = service.verify(request, correlation_id=f"regression-{product['id']}")
        record = result_record(product, result, time.monotonic() - started)
        if config["mode"] == "cold":
            record["cache_state"] = "force_refresh"
        output_queue.put((product["id"], record))
    except BaseException as error:  # process boundary: always return a usable record
        output_queue.put((product["id"], _failure_record(
            product, runtime_seconds=time.monotonic() - started,
            error_kind="worker_error", message=f"{type(error).__name__}: {error}",
        )))


def aggregate_metrics(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    statuses = Counter(record.get("quality_status") for record in records if record.get("quality_status"))
    category_distribution = Counter(
        record.get("category") or "unclassified" for record in records
    )
    by_category: dict[str, Counter[str]] = defaultdict(Counter)
    for record in records:
        category = str(record.get("category") or "unclassified")
        status = str(record.get("quality_status") or "failed")
        by_category[category][status] += 1
    coverages = [float(r["coverage_percent"]) for r in records if r.get("coverage_percent") is not None]
    runtimes = [float(r["runtime_seconds"]) for r in records if r.get("runtime_seconds") is not None]
    timed_out = sum(bool(r.get("timed_out")) for r in records)
    successful = sum(bool(r.get("final_success")) for r in records)
    failed = len(records) - successful - timed_out
    deterministic = sum(
        not r.get("final_success") and not r.get("timed_out")
        and not r.get("blocking_marker") for r in records
    )
    external = sum(_record_is_external(r) for r in records)
    low_evidence = sum(
        bool(r.get("final_success")) and r.get("quality_status") == "insufficient"
        and not _record_is_external(r)
        for r in records
    )
    reason_counter: Counter[str] = Counter()
    for record in records:
        if record.get("error_kind"):
            reason_counter[f"error:{record['error_kind']}"] += 1
        for reason in record.get("quality_reasons", []):
            reason_counter[f"quality:{reason}"] += 1
    hits = sum(bool(r.get("served_from_cache")) for r in records)
    return {
        "total_products": len(records), "completed": successful, "failed": failed,
        "timed_out": timed_out,
        **{status: statuses.get(status, 0) for status in sorted(QUALITY_STATUSES)},
        "success_rate_percent": round(100.0 * successful / len(records), 2) if records else 0.0,
        "average_coverage_percent": round(statistics.fmean(coverages), 2) if coverages else None,
        "median_coverage_percent": round(statistics.median(coverages), 2) if coverages else None,
        "average_runtime_seconds": round(statistics.fmean(runtimes), 3) if runtimes else None,
        "median_runtime_seconds": round(statistics.median(runtimes), 3) if runtimes else None,
        "category_distribution": dict(sorted(category_distribution.items())),
        "status_distribution_by_category": {
            category: dict(sorted(counts.items())) for category, counts in sorted(by_category.items())
        },
        "cache_hit_rate_percent": round(100.0 * hits / len(records), 2) if records else 0.0,
        "external_blocking_or_fetch_failure_rate_percent": (
            round(100.0 * external / len(records), 2) if records else 0.0
        ),
        "failure_groups": {
            "deterministic_application_failure": deterministic,
            "external_network_or_blocking_instability": external,
            "low_evidence_result": low_evidence,
        },
        "top_failure_or_reason_groups": [
            {"reason": reason, "count": count} for reason, count in reason_counter.most_common(10)
        ],
    }


def load_baseline(path: str | Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("products"), list):
        raise ValueError("baseline must contain a products list")
    return data


def compare_references(
    records: Sequence[Mapping[str, Any]], baseline: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    if baseline is None:
        return []
    current = {record["product_id"]: record for record in records}
    comparisons: list[dict[str, Any]] = []
    for previous in baseline["products"]:
        record = current.get(previous["product_id"])
        coverage = record.get("coverage_percent") if record else None
        delta = (
            round(float(coverage) - float(previous["stage9_coverage_percent"]), 2)
            if coverage is not None else None
        )
        external = bool(record and _record_is_external(record))
        expected = previous.get("expected_category")
        category_mismatch = bool(record and record.get("category") and record.get("category") != expected)
        if record is None:
            assessment = "not_run"
        elif external:
            assessment = "live_variability_or_external_failure"
        elif not record.get("final_success"):
            assessment = "needs_deterministic_review"
        elif category_mismatch or (delta is not None and delta < -10.0):
            assessment = "needs_deterministic_review"
        else:
            assessment = "no_deterministic_regression_signal"
        comparisons.append({
            "product_id": previous["product_id"],
            "previous_coverage_percent": previous["stage9_coverage_percent"],
            "historical_coverage": previous.get("historical_coverage"),
            "current_coverage_percent": coverage,
            "current_quality_status": record.get("quality_status") if record else None,
            "delta_percentage_points": delta,
            "assessment": assessment,
        })
    return comparisons


def atomic_write_json(path: str | Path, payload: Mapping[str, Any]) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(destination)


def resumable_products(
    products: Sequence[dict[str, Any]], existing_records: Sequence[Mapping[str, Any]],
    *, retry_failures: bool,
) -> list[dict[str, Any]]:
    previous = {record.get("product_id"): record for record in existing_records}
    selected = []
    for product in products:
        record = previous.get(product["id"])
        if record is None:
            selected.append(product)
        elif not record.get("final_success") and retry_failures:
            selected.append(product)
    return selected


@dataclass
class _Running:
    product: dict[str, Any]
    process: Any
    output_queue: Any
    started: float


def run_products(
    products: Sequence[dict[str, Any]], config: dict[str, Any],
    on_record: Callable[[dict[str, Any]], None], *,
    worker_target: Callable[[dict[str, Any], dict[str, Any], Any], None] = _worker,
) -> None:
    """Run products with bounded concurrency and a killable hard timeout."""
    concurrency = int(config["concurrency"])
    timeout_seconds = float(config["timeout_seconds"])
    if concurrency < 1:
        raise ValueError("concurrency must be at least 1")
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    context = multiprocessing.get_context("spawn")
    pending = iter(products)
    running: dict[str, _Running] = {}
    exhausted = False
    try:
        while running or not exhausted:
            while len(running) < concurrency and not exhausted:
                try:
                    product = next(pending)
                except StopIteration:
                    exhausted = True
                    break
                output_queue = context.Queue()
                process = context.Process(
                    target=worker_target, args=(product, config, output_queue),
                    name=f"regression-{product['id']}", daemon=False,
                )
                process.start()
                running[product["id"]] = _Running(
                    product, process, output_queue, time.monotonic(),
                )

            for product_id, item in list(running.items()):
                try:
                    returned_id, record = item.output_queue.get_nowait()
                except queue.Empty:
                    continue
                if returned_id != product_id:
                    raise RuntimeError(
                        f"worker for {product_id} returned result for {returned_id}"
                    )
                running.pop(product_id)
                item.process.join(timeout=1.0)
                if item.process.is_alive():
                    item.process.terminate()
                    item.process.join(timeout=2.0)
                item.output_queue.close()
                item.output_queue.join_thread()
                on_record(record)

            now = time.monotonic()
            for product_id, item in list(running.items()):
                elapsed = now - item.started
                if elapsed >= timeout_seconds:
                    item.process.terminate()
                    item.process.join(timeout=2.0)
                    running.pop(product_id, None)
                    item.output_queue.close()
                    item.output_queue.join_thread()
                    on_record(timeout_record(item.product, timeout_seconds))
                elif not item.process.is_alive():
                    item.process.join(timeout=0.1)
                    running.pop(product_id, None)
                    try:
                        returned_id, record = item.output_queue.get(timeout=0.25)
                    except queue.Empty:
                        record = _failure_record(
                            item.product, runtime_seconds=elapsed,
                            error_kind="worker_exit",
                            message=f"worker exited with code {item.process.exitcode} without a result",
                        )
                    else:
                        if returned_id != product_id:
                            raise RuntimeError(
                                f"worker for {product_id} returned result for {returned_id}"
                            )
                    item.output_queue.close()
                    item.output_queue.join_thread()
                    on_record(record)
            if running or not exhausted:
                time.sleep(0.05)
    finally:
        for item in running.values():
            if item.process.is_alive():
                item.process.terminate()
            item.process.join(timeout=2.0)
            item.output_queue.close()
            item.output_queue.join_thread()


def _select_products(
    products: Sequence[dict[str, Any]], *, tag: str | None, limit: int | None,
) -> list[dict[str, Any]]:
    selected = [product for product in products if tag is None or tag in product.get("tags", [])]
    return selected[:limit] if limit is not None else selected


def run(args: argparse.Namespace) -> dict[str, Any]:
    dataset = load_dataset(args.dataset)
    products = _select_products(dataset["products"], tag=args.tag, limit=args.limit)
    if not products:
        raise ValueError("selection produced no products")
    baseline = load_baseline(args.baseline)
    output = Path(args.output)
    prior: dict[str, Any] | None = None
    if args.resume and output.exists():
        prior = json.loads(output.read_text(encoding="utf-8"))
    existing = list(prior.get("records", [])) if prior else []
    pending = resumable_products(products, existing, retry_failures=not args.keep_failures)
    selected_ids = {product["id"] for product in products}
    records_by_id = {
        record["product_id"]: record for record in existing
        if record.get("product_id") in selected_ids
    }
    reused_successes = sum(
        bool(record.get("final_success")) for record in records_by_id.values()
    )
    order = {product["id"]: index for index, product in enumerate(products)}
    current_records = sorted(
        records_by_id.values(), key=lambda value: order[value["product_id"]],
    )
    config = {
        "mode": args.mode, "concurrency": args.concurrency,
        "timeout_seconds": args.timeout, "cache_db": str(Path(args.cache_db)),
        "cache_ttl_seconds": args.cache_ttl,
        "max_sources": args.max_sources,
        "targeted_search_enabled": not args.no_targeted_search,
    }
    artifact: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "run_id": prior.get("run_id", str(uuid.uuid4())) if prior else str(uuid.uuid4()),
        "started_at": prior.get("started_at", utc_now()) if prior else utc_now(),
        "finished_at": None,
        "dataset": {
            "id": dataset.get("dataset_id"), "path": str(Path(args.dataset)),
            "total_size": len(dataset["products"]), "selected_size": len(products),
            "tag": args.tag,
        },
        "config": config,
        "resume": {
            "enabled": bool(args.resume), "retry_failures": not args.keep_failures,
            "reused_successful_records": reused_successes,
            "last_resumed_at": utc_now() if prior else None,
        },
        "records": current_records,
        "aggregate": aggregate_metrics(current_records),
        "reference_comparison": compare_references(current_records, baseline),
    }

    def save(record: dict[str, Any]) -> None:
        records_by_id[record["product_id"]] = record
        records = sorted(records_by_id.values(), key=lambda value: order[value["product_id"]])
        artifact["records"] = records
        artifact["aggregate"] = aggregate_metrics(records)
        artifact["reference_comparison"] = compare_references(records, baseline)
        atomic_write_json(output, artifact)

    atomic_write_json(output, artifact)
    run_products(pending, config, save)
    artifact["finished_at"] = utc_now()
    artifact["records"] = sorted(records_by_id.values(), key=lambda value: order[value["product_id"]])
    artifact["aggregate"] = aggregate_metrics(artifact["records"])
    artifact["reference_comparison"] = compare_references(artifact["records"], baseline)
    atomic_write_json(output, artifact)
    return artifact


def build_parser() -> argparse.ArgumentParser:
    root = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", default=str(root / "datasets" / "mvp_50_v1.json"))
    parser.add_argument("--baseline", default=str(root / "baselines" / "reference_v1.json"))
    parser.add_argument("--output", default=str(root / "results" / "latest.json"))
    parser.add_argument("--cache-db", default=str(root / "results" / "benchmark-cache.sqlite3"))
    parser.add_argument("--mode", choices=("cold", "warm"), default="cold")
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--cache-ttl", type=float, default=3600.0)
    parser.add_argument("--max-sources", type=int, default=5)
    parser.add_argument("--no-targeted-search", action="store_true")
    parser.add_argument("--tag", help="run only products carrying this dataset tag")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument(
        "--keep-failures", action="store_true",
        help="with --resume, keep prior failures/timeouts instead of retrying them",
    )
    parser.add_argument("--validate-only", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.limit is not None and args.limit < 1:
        raise SystemExit("--limit must be positive")
    dataset = load_dataset(args.dataset)
    if args.validate_only:
        categories = Counter(item.get("expected_category", "unknown") for item in dataset["products"])
        print(json.dumps({
            "dataset_id": dataset.get("dataset_id"), "products": len(dataset["products"]),
            "category_distribution": dict(sorted(categories.items())), "valid": True,
        }, ensure_ascii=False, indent=2))
        return 0
    artifact = run(args)
    print(json.dumps({
        "run_id": artifact["run_id"], "output": args.output,
        "aggregate": artifact["aggregate"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
