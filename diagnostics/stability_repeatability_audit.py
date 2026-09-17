"""Cross-run provider stability and repeatability audit for live artifacts."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path
import statistics
from typing import Any, Iterable, Mapping, Sequence

from core.discovery import canonicalize_url
from diagnostics.retrieval_conversion_audit import audit_artifact


FIRST_PARTY_TYPES = {"manufacturer", "official_document"}
NON_REQUEST_STATUSES = {"circuit_open", "capped"}


def _official(item: Mapping[str, Any]) -> bool:
    return (
        item.get("authority_status") == "verified"
        and item.get("source_type") in FIRST_PARTY_TYPES
    )


def _exact(item: Mapping[str, Any]) -> bool:
    return (
        item.get("model_match") == "exact"
        or item.get("model_relevance") == "exact_base_model"
        or item.get("identity_relation") in {"same_base_model", "exact_variant"}
    ) and item.get("identity_relation") != "different_model"


def _candidate_providers(items: Iterable[Mapping[str, Any]]) -> list[str]:
    providers: set[str] = set()
    for item in items:
        provider = str(item.get("discovery_provider") or "").strip()
        if provider:
            providers.add(provider)
        for provenance in item.get("discovery_provenance") or ():
            provider = str(provenance.get("provider") or "").strip()
            if provider:
                providers.add(provider)
    return sorted(providers)


def _variance(values: Sequence[float]) -> float:
    return round(statistics.pvariance(values), 4) if len(values) > 1 else 0.0


def _rate(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 4) if denominator else 0.0


def _jump_over_half(values: Sequence[int]) -> bool:
    if not values or max(values) == 0:
        return False
    low, high = min(values), max(values)
    return (high - low) / max(1, low) > 0.5


def _repeatability_score(
    confirmed_rate_spread: float,
    coverage_spread: float,
    quality_change_rate: float,
    confirmed_jump_rate: float,
) -> float:
    """Return a transparent 0..100 stability score (100 means no variance)."""
    components = (
        1.0 - min(1.0, confirmed_rate_spread),
        1.0 - min(1.0, coverage_spread / 100.0),
        1.0 - min(1.0, quality_change_rate),
        1.0 - min(1.0, confirmed_jump_rate),
    )
    return round(statistics.fmean(components) * 100.0, 2)


def audit_artifacts(
    artifacts: Sequence[Mapping[str, Any]],
    labels: Sequence[str] | None = None,
) -> dict[str, Any]:
    if len(artifacts) < 2:
        raise ValueError("repeatability audit requires at least two artifacts")
    run_labels = list(labels or (f"run-{index + 1}" for index in range(len(artifacts))))
    if len(run_labels) != len(artifacts) or len(set(run_labels)) != len(run_labels):
        raise ValueError("labels must be unique and match the number of artifacts")

    single = [audit_artifact(artifact) for artifact in artifacts]
    row_maps = [
        {row["id"]: row for row in report["rows"]}
        for report in single
    ]
    product_ids = list(row_maps[0])
    if any(set(rows) != set(product_ids) for rows in row_maps[1:]):
        raise ValueError("all artifacts must contain the same product ids")

    cross_rows: list[dict[str, Any]] = []
    provider_totals: dict[str, Counter[str]] = defaultdict(Counter)
    provider_runtime: Counter[str] = Counter()
    provider_contributions: Counter[str] = Counter()
    run_metrics: list[dict[str, Any]] = []

    for label, artifact, report, rows in zip(run_labels, artifacts, single, row_maps):
        aggregate = dict(report["aggregate"])
        aggregate["run"] = label
        aggregate["exact_official_discovery_rate"] = _rate(
            aggregate["funnel"]["exact_official_candidate_identified"],
            aggregate["products"],
        )
        run_metrics.append(aggregate)
        records = {item["product_id"]: item for item in artifact.get("records", ())}
        for product_id in product_ids:
            row = rows[product_id]
            record = records[product_id]
            trace = record.get("trace") or {}
            discovery = trace.get("discovery") or {}
            candidates = [
                *discovery.get("accepted_candidates", ()),
                *discovery.get("rejected_candidates", ()),
            ]
            exact_official = [item for item in candidates if _official(item) and _exact(item)]
            providers = _candidate_providers(exact_official)
            for provider in providers:
                provider_contributions[provider] += 1
            selected = (trace.get("relevance_and_ranking") or {}).get("selected", ())
            fetches = (trace.get("fetch") or {}).get("sources", ())
            cross_rows.append({
                "run": label,
                "product_id": product_id,
                "category": row.get("category"),
                "official_candidate_discovered": row["official_discovered"],
                "exact_official_candidate_discovered": row["exact_official_identified"],
                "exact_official_providers": providers,
                "selected_for_fetch": row["exact_official_selected"],
                "fetch_attempted": row["fetch_attempted"],
                "fetch_succeeded": row["fetch_succeeded"],
                "fetch_statuses": dict(Counter(item.get("status") for item in fetches)),
                "selected_urls": sorted({
                    canonicalize_url(str(item.get("url") or "")) for item in selected
                    if canonicalize_url(str(item.get("url") or ""))
                }),
                "raw": row["raw"],
                "mapped": row["expected_mapped"],
                "confirmed": row["confirmed"],
                "unresolved": row["unresolved"],
                "conflicts": row["conflicts"],
                "coverage": row["coverage"],
                "quality": row["quality"],
                "runtime": row["runtime"],
                "dominant_failure": row["dominant_failure"],
            })
            for attempt in discovery.get("provider_attempts", ()):
                provider = str(attempt.get("provider") or "unknown")
                status = str(attempt.get("status") or "error")
                provider_totals[provider]["recorded_attempts"] += 1
                provider_totals[provider][status] += 1
                if status not in NON_REQUEST_STATUSES:
                    provider_totals[provider]["request_attempts"] += 1
                provider_totals[provider]["result_count"] += int(
                    attempt.get("result_count") or 0
                )
                provider_runtime[provider] += float(attempt.get("duration_seconds") or 0.0)
                if attempt.get("shared_circuit_open"):
                    provider_totals[provider]["shared_circuit_open_count"] += 1
                if attempt.get("retried"):
                    provider_totals[provider]["retried_count"] += 1
                failure_class = attempt.get("failure_class")
                if failure_class:
                    provider_totals[provider][f"failure_class:{failure_class}"] += 1

    per_product: list[dict[str, Any]] = []
    for product_id in product_ids:
        rows = [row_map[product_id] for row_map in row_maps]
        confirmed = [int(row["confirmed"]) for row in rows]
        coverage = [float(row["coverage"]) for row in rows]
        qualities = [row.get("quality") for row in rows]
        per_product.append({
            "product_id": product_id,
            "confirmed": confirmed,
            "confirmed_variance": _variance(confirmed),
            "confirmed_jump_over_50_percent": _jump_over_half(confirmed),
            "coverage": coverage,
            "coverage_variance": _variance(coverage),
            "quality": qualities,
            "quality_changed": len(set(qualities)) > 1,
        })

    product_count = len(product_ids)
    quality_changed = sum(item["quality_changed"] for item in per_product)
    confirmed_jumped = sum(
        item["confirmed_jump_over_50_percent"] for item in per_product
    )
    confirmed_rates = [float(item["confirmed_gt_0_rate"]) for item in run_metrics]
    median_coverages = [float(item["median_coverage"]) for item in run_metrics]
    confirmed_rate_spread = round(max(confirmed_rates) - min(confirmed_rates), 4)
    coverage_spread = round(max(median_coverages) - min(median_coverages), 4)
    quality_change_rate = _rate(quality_changed, product_count)
    confirmed_jump_rate = _rate(confirmed_jumped, product_count)

    provider_metrics = []
    for provider in sorted(provider_totals):
        counts = provider_totals[provider]
        requests = counts["request_attempts"]
        provider_metrics.append({
            "provider": provider,
            **dict(sorted(counts.items())),
            "success_rate": _rate(counts["success"], requests),
            "failure_rate": _rate(
                sum(counts[status] for status in (
                    "timeout", "blocked", "empty", "parse_error", "error", "low_value",
                )),
                requests,
            ),
            "exact_official_product_run_contribution": provider_contributions[provider],
            "runtime_seconds": round(provider_runtime[provider], 4),
        })

    stability = {
        "confirmed_rate_spread": confirmed_rate_spread,
        "median_coverage_spread_percentage_points": coverage_spread,
        "quality_changed_products": quality_changed,
        "quality_change_rate": quality_change_rate,
        "confirmed_jump_over_50_percent_products": confirmed_jumped,
        "confirmed_jump_over_50_percent_rate": confirmed_jump_rate,
        "repeatability_score": _repeatability_score(
            confirmed_rate_spread,
            coverage_spread,
            quality_change_rate,
            confirmed_jump_rate,
        ),
        "score_formula": (
            "mean(1-confirmed-rate-spread, 1-coverage-spread/100, "
            "1-quality-change-rate, 1-confirmed-jump-rate) * 100"
        ),
    }
    resilience = {
        "shared_circuit_open_attempts": sum(
            item.get("shared_circuit_open_count", 0) for item in provider_metrics
        ),
        "retried_attempts": sum(
            item.get("retried_count", 0) for item in provider_metrics
        ),
        "provider_health_snapshots": [
            artifact["provider_health_snapshot"]
            for artifact in artifacts
            if artifact.get("provider_health_snapshot")
        ],
    }
    return {
        "schema_version": "1.0",
        "runs": run_metrics,
        "stability": stability,
        "providers": provider_metrics,
        "resilience": resilience,
        "per_product": per_product,
        "cross_run_rows": cross_rows,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifacts", nargs="+")
    parser.add_argument("--labels", help="comma-separated labels in artifact order")
    parser.add_argument("--output")
    args = parser.parse_args(argv)
    artifacts = [json.loads(Path(path).read_text(encoding="utf-8")) for path in args.artifacts]
    labels = args.labels.split(",") if args.labels else None
    result = audit_artifacts(artifacts, labels)
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
