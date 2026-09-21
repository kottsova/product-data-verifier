"""Compare a Stage 36.7 run with the Stage 36.6 name-only baseline.

Network blocks (bot checks, HTTP 403/429, timeouts) are counted separately from
program faults (worker crash, leftover processes, EPIPE, an exception in the
row).  A status column is reported as measured; nothing here re-adjudicates
whether an accepted page is correct - new accepted URLs are listed for manual
review instead.
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

from diagnostics.stage36_7_time_audit import BUDGET, load_rows

BASELINE = Path("diagnostics/baselines/stage36_6/full_name_only_20260920/raw_sanitized.zip")
NET_BLOCK_CLASSES = {"blocked", "rate_limited"}
NET_TIMEOUT_CLASSES = {"timeout", "connection_error"}


def _urlkey(url: str) -> str:
    return url.split("://", 1)[-1].removeprefix("www.").rstrip("/").lower()


def measure(row: dict) -> dict:
    attempts = (row.get("trace") or {}).get("provider_attempts", [])
    executed = [a for a in attempts if a["status"] not in {"circuit_open", "capped"}]
    fetches = row.get("page_fetches") or []
    isolation = (row.get("performance") or {}).get("isolation") or {}
    http_403 = sum(1 for f in fetches for a in f.get("attempts", []) if a.get("http_status") == 403)
    http_429 = sum(1 for f in fetches for a in f.get("attempts", []) if a.get("http_status") == 429)
    blocks = sum(1 for a in executed if a.get("blocked") or a.get("failure_class") in NET_BLOCK_CLASSES)
    net_timeouts = sum(1 for a in executed if a["status"] == "timeout" and not a.get("budget_exhausted")
                       and a.get("failure_reason") != "hard_deadline")
    other_errors = [a for a in executed if a["status"] == "error"]
    program_faults = []
    if row.get("error"):
        program_faults.append(f"row_exception:{row['error'][:80]}")
    if isolation.get("worker_error"):
        program_faults.append(f"worker_error:{isolation['worker_error']}")
    if isolation.get("leftover_process_count"):
        program_faults.append(f"leftover_processes:{isolation['leftover_process_count']}")
    if isolation.get("worker_stderr_epipe"):
        program_faults.append("epipe_in_worker_stderr")
    exact_pages = [p for p in row.get("official_pages", []) if p.get("model_match") == "exact"]
    return {
        "status": row.get("status", "ERROR"),
        "runtime_seconds": row.get("runtime_seconds") or row.get("harness_elapsed_seconds") or 0.0,
        "over_75": (row.get("runtime_seconds") or row.get("harness_elapsed_seconds") or 0.0) > BUDGET,
        "candidates_unique": (row.get("candidate_counts") or {}).get("unique", 0),
        "queries_attempted": len(row.get("attempted_queries") or []),
        "provider_calls_executed": len(executed),
        "page_fetches": len(fetches),
        "pages_loaded": sum(1 for f in fetches if f.get("status") == "loaded"),
        "official_pages": len(row.get("official_pages") or []),
        "exact_official_pages": len(exact_pages),
        "accepted_urls": sorted(_urlkey(p["url"]) for p in exact_pages),
        "support_pages": len(row.get("support_pages") or []),
        "documents": len(row.get("documents") or []),
        "net_provider_blocks": blocks,
        "net_http_403": http_403,
        "net_http_429": http_429,
        "net_provider_timeouts": net_timeouts,
        "provider_other_errors": len(other_errors),
        "program_faults": ";".join(program_faults),
        "hard_stop": bool(isolation.get("hard_stop")),
        "stop_reason": isolation.get("stop_reason") or "",
        "stopped_in": json.dumps(isolation.get("stopped_in") or {}, ensure_ascii=False),
        "post_result_cleanup_seconds": isolation.get("post_result_cleanup_seconds"),
    }


def compare(new_dir: Path, output: Path) -> dict:
    old_rows = {r["index"]: r for r in load_rows(BASELINE)}
    new_rows = {r["index"]: r for r in load_rows(new_dir)}
    records = []
    for index in sorted(new_rows):
        old, new = measure(old_rows[index]), measure(new_rows[index])
        record = {"index": index, "input": new_rows[index]["input"]}
        for key, value in old.items():
            record[f"old_{key}"] = value
        for key, value in new.items():
            record[f"new_{key}"] = value
        record["new_accepted_not_in_old"] = ";".join(
            sorted(set(new["accepted_urls"]) - set(old["accepted_urls"])))
        record["old_accepted_not_in_new"] = ";".join(
            sorted(set(old["accepted_urls"]) - set(new["accepted_urls"])))
        records.append(record)
    output.mkdir(parents=True, exist_ok=True)
    with (output / "comparison.csv").open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(records[0]))
        writer.writeheader()
        writer.writerows(records)

    def total(prefix: str, key: str) -> float:
        return round(sum(float(r[f"{prefix}_{key}"] or 0) for r in records), 1)

    def count(prefix: str, key: str, value) -> int:
        return sum(1 for r in records if r[f"{prefix}_{key}"] == value)

    summary = {"products": len(records)}
    for prefix in ("old", "new"):
        runtimes = [r[f"{prefix}_runtime_seconds"] for r in records]
        summary[prefix] = {
            "PASS": count(prefix, "status", "PASS"), "PARTIAL": count(prefix, "status", "PARTIAL"),
            "FAIL": count(prefix, "status", "FAIL"),
            "over_75s": sum(1 for r in records if r[f"{prefix}_over_75"]),
            "max_runtime_seconds": max(runtimes),
            "median_runtime_seconds": sorted(runtimes)[len(runtimes) // 2],
            "total_runtime_seconds": round(sum(runtimes), 1),
            "unique_candidates": total(prefix, "candidates_unique"),
            "zero_candidate_products": sum(1 for r in records if not r[f"{prefix}_candidates_unique"]),
            "queries_attempted": total(prefix, "queries_attempted"),
            "page_fetches": total(prefix, "page_fetches"), "pages_loaded": total(prefix, "pages_loaded"),
            "exact_official_pages": total(prefix, "exact_official_pages"),
            "support_pages": total(prefix, "support_pages"), "documents": total(prefix, "documents"),
            "net_provider_blocks": total(prefix, "net_provider_blocks"),
            "net_http_403": total(prefix, "net_http_403"), "net_http_429": total(prefix, "net_http_429"),
            "net_provider_timeouts": total(prefix, "net_provider_timeouts"),
            "provider_other_errors": total(prefix, "provider_other_errors"),
            "program_fault_rows": sum(1 for r in records if r[f"{prefix}_program_faults"]),
        }
    summary["new"]["hard_stops"] = sum(1 for r in records if r["new_hard_stop"])
    summary["new"]["max_over_budget_seconds"] = round(max(
        max(0.0, r["new_runtime_seconds"] - BUDGET) for r in records), 3)
    summary["new"]["max_post_result_cleanup_seconds"] = max(
        (r["new_post_result_cleanup_seconds"] or 0.0) for r in records)
    summary["status_changes"] = [
        {"index": r["index"], "input": r["input"], "old": r["old_status"], "new": r["new_status"]}
        for r in records if r["old_status"] != r["new_status"]]
    summary["accepted_lost"] = [
        {"index": r["index"], "input": r["input"], "urls": r["old_accepted_not_in_new"]}
        for r in records if r["old_accepted_not_in_new"]]
    summary["accepted_new_needing_manual_review"] = [
        {"index": r["index"], "input": r["input"], "urls": r["new_accepted_not_in_old"]}
        for r in records if r["new_accepted_not_in_old"]]
    (output / "comparison_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--new", required=True, type=Path, help="directory of NN.json rows")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(compare(args.new, args.output), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
