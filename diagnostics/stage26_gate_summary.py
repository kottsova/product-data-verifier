"""Summarize Stage 26 live artifacts into acceptance-gate metrics."""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
from statistics import mean, median
from typing import Any, Sequence


def summarize_artifact(path: str) -> dict[str, Any]:
    artifact = json.loads(Path(path).read_text(encoding="utf-8"))
    records = artifact["records"]
    qualities = [item["trace"]["quality"] for item in records]
    coverages = [float(item["coverage_percent"]) for item in qualities]
    attempts = [
        attempt
        for item in records
        for attempt in item["trace"]["discovery"]["provider_attempts"]
    ]
    accepted_providers: Counter[str] = Counter()
    independent_providers: Counter[str] = Counter()
    provider_failures: Counter[str] = Counter()
    for item in records:
        discovery = item["trace"]["discovery"]
        for candidate in discovery["accepted_candidates"]:
            for provenance in candidate.get("discovery_provenance") or ():
                provider = str(provenance.get("provider") or "unknown")
                accepted_providers[provider] += 1
        for attempt in discovery["provider_attempts"]:
            provider = str(attempt.get("provider") or "unknown")
            if attempt.get("independent_success_without_ddg"):
                independent_providers[provider] += 1
            if attempt.get("status") in {
                "blocked", "timeout", "parse_error", "error", "low_value",
            }:
                provider_failures[provider] += 1
    return {
        "artifact": path,
        "products": len(records),
        "completed": sum(not item["timed_out"] for item in records),
        "service_success": sum(bool(item["service_success"]) for item in records),
        "quality_statuses": dict(Counter(item["status"] for item in qualities)),
        "confirmed_gt_0": sum(item["confirmed_count"] > 0 for item in qualities),
        "average_coverage": round(mean(coverages), 4),
        "median_coverage": round(median(coverages), 4),
        "runtime_seconds": round(sum(float(item["runtime_seconds"]) for item in records), 4),
        "full_retrieval_collapses": sum(
            item["schema_found"] == 0 and item["coverage_percent"] == 0.0
            for item in qualities
        ),
        "total_provider_failures": sum(provider_failures.values()),
        "provider_failures": dict(provider_failures),
        "accepted_candidate_providers": dict(accepted_providers),
        "independent_success_attempts": dict(independent_providers),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifacts", nargs="+")
    parser.add_argument("--output")
    args = parser.parse_args(argv)
    result = {"runs": [summarize_artifact(path) for path in args.artifacts]}
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
