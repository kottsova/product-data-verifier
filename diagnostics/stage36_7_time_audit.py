"""Stage 36.7 time audit: where do the 75 seconds of a discovery call go?

Reads a saved raw archive (a directory of ``NN.json`` files or a ``.zip``) and,
for every product, splits the recorded runtime into provider stages and page
loads and names the single call that crossed the wall-clock budget.

Timeline reconstruction uses only recorded fields: a provider attempt started at
``budget - budget_before_seconds`` and ended at ``budget - budget_after_seconds``
(both clamped at the budget), so the call that straddles t = budget is the one
that exceeded it.  What the trace does *not* record (PDF reads, browser and
driver shutdown, thread-pool joins) shows up as ``unattributed_seconds`` and is
never presented as a measurement.
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
import zipfile

BUDGET = 75.0
SEARCH_PROVIDERS = {"duckduckgo_html", "duckduckgo_lite", "bing", "google", "naver", "seznam"}
DOCUMENT_QUERY_MARKERS = ("user manual pdf", "declaration of conformity")
# A statuses whose attempt row copies telemetry without having run the provider.
NOT_EXECUTED = {"circuit_open", "capped"}


def load_rows(source: Path) -> list[dict]:
    if source.suffix == ".zip":
        with zipfile.ZipFile(source) as archive:
            names = sorted(name for name in archive.namelist() if name.endswith(".json"))
            return [json.loads(archive.read(name).decode("utf-8-sig")) for name in names]
    return [json.loads(path.read_text(encoding="utf-8-sig"))
            for path in sorted(source.glob("[0-9][0-9].json"))]


def _stage(attempt: dict) -> str:
    provider = attempt["provider"]
    if any(marker in (attempt.get("query") or "") for marker in DOCUMENT_QUERY_MARKERS):
        return "document_queries"
    if provider == "direct_domain_probe":
        return "direct_probes"
    if provider == "browser_official_discovery":
        return "browser"
    if provider in SEARCH_PROVIDERS:
        return "search_providers"
    return "other_providers"


def _interval(attempt: dict, budget: float) -> tuple[float, float]:
    duration = attempt.get("duration_seconds") or 0.0
    before, after = attempt.get("budget_before_seconds"), attempt.get("budget_after_seconds")
    start = budget - before if before is not None else 0.0
    end = start + duration
    if after is not None and after > 0.0:
        end = max(end, budget - after)
    return round(start, 3), round(end, 3)


def analyse(row: dict, budget: float = BUDGET) -> dict:
    runtime = row.get("runtime_seconds") or row.get("harness_elapsed_seconds") or 0.0
    attempts = (row.get("trace") or {}).get("provider_attempts", [])
    executed = [a for a in attempts if a["status"] not in NOT_EXECUTED and (a.get("duration_seconds") or 0) > 0]
    stages = {name: 0.0 for name in (
        "search_providers", "direct_probes", "browser", "document_queries", "other_providers")}
    for attempt in executed:
        stages[_stage(attempt)] += attempt.get("duration_seconds") or 0.0
    fetches = row.get("page_fetches") or []
    page_seconds = sum(item.get("duration_seconds") or 0.0 for item in fetches)
    provider_seconds = sum(stages.values())
    out = {
        "index": row.get("index"), "input": row.get("input"), "runtime_seconds": runtime,
        "over_budget": runtime > budget,
        "over_budget_seconds": round(max(0.0, runtime - budget), 3),
        "search_status": row.get("search_status"),
        "unique_candidates": (row.get("candidate_counts") or {}).get("unique", 0),
        **{f"{name}_seconds": round(value, 3) for name, value in stages.items()},
        "page_load_seconds_sum": round(page_seconds, 3),
        "page_load_count": len(fetches),
        # Page loads run four at a time, so their sum is an upper bound; the
        # residual below is therefore a lower bound of untracked time.
        "unattributed_seconds": round(max(0.0, runtime - provider_seconds - page_seconds), 3),
    }
    # The call that crossed the budget.
    crossing = None
    for attempt in executed:
        start, end = _interval(attempt, budget)
        if start < budget < end + 1e-9 or (start < budget and end >= budget):
            crossing = (attempt, start, end)
    if crossing is None and executed:
        last = max(executed, key=lambda item: _interval(item, budget)[1])
        start, end = _interval(last, budget)
        if end > budget:
            crossing = (last, start, end)
    tail = None
    if crossing is not None:
        attempt, start, end = crossing
        tail = round(max(0.0, runtime - end), 3)
        out.update(
            crossing_provider=attempt["provider"], crossing_status=attempt["status"],
            crossing_query=attempt.get("query"), crossing_start_seconds=start,
            crossing_duration_seconds=attempt.get("duration_seconds"),
            crossing_configured_timeout=attempt.get("timeout_seconds"),
            crossing_overshoot_of_own_timeout=round(
                max(0.0, (attempt.get("duration_seconds") or 0.0) - (attempt.get("timeout_seconds") or 0.0)), 3),
            crossing_failure_reason=attempt.get("failure_reason"),
            tail_after_crossing_call_seconds=tail,
        )
    elif out["over_budget"]:
        out["crossing_provider"] = "no_recorded_provider_call"
        out["tail_after_crossing_call_seconds"] = None
    # Slowest single call regardless of budget.
    if executed:
        slowest = max(executed, key=lambda item: item.get("duration_seconds") or 0.0)
        out.update(slowest_provider=slowest["provider"], slowest_status=slowest["status"],
                   slowest_seconds=slowest.get("duration_seconds"),
                   slowest_timeout=slowest.get("timeout_seconds"))
    if fetches:
        long_fetch = max(fetches, key=lambda item: item.get("duration_seconds") or 0.0)
        out.update(slowest_page_seconds=long_fetch.get("duration_seconds"),
                   slowest_page_status=long_fetch.get("status"))
    return out


def write_reports(rows: list[dict], output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    analysed = [analyse(row) for row in rows]
    keys: list[str] = []
    for item in analysed:
        for key in item:
            if key not in keys:
                keys.append(key)
    with (output / "time_by_stage.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=keys)
        writer.writeheader()
        writer.writerows(analysed)
    over = [item for item in analysed if item["over_budget"]]
    zero = [item for item in analysed if item["unique_candidates"] == 0]
    summary = {
        "products": len(analysed), "over_budget": len(over),
        "max_runtime_seconds": max(item["runtime_seconds"] for item in analysed),
        "totals_seconds": {name: round(sum(item.get(f"{name}_seconds", 0) for item in analysed), 1)
                           for name in ("search_providers", "direct_probes", "browser",
                                        "document_queries", "other_providers", "page_load")
                           if name != "page_load"} | {
            "page_load": round(sum(item["page_load_seconds_sum"] for item in analysed), 1),
            "unattributed": round(sum(item["unattributed_seconds"] for item in analysed), 1)},
        "crossing_provider_counts": _count(item.get("crossing_provider") for item in over),
        "zero_candidate_products": [item["input"] for item in zero],
    }
    (output / "time_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


def _count(values) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        counts[str(value)] = counts.get(str(value), 0) + 1
    return dict(sorted(counts.items(), key=lambda pair: -pair[1]))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    write_reports(load_rows(args.archive), args.output)


if __name__ == "__main__":
    main()
