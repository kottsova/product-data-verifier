"""Compare identical selected inputs without category before/after scope fix.

The guided runs are retained as a separate counterfactual: they supplied the
benchmark category and were collected earlier under different network state.
"""

from __future__ import annotations

import argparse
from collections import Counter
import csv
import json
from pathlib import Path


def _read(path: Path) -> dict[int, dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return {int(row["index"]): row for row in data["rows"]}


def compare(before: Path, after: Path, guided: list[Path], output: Path,
            after_updates: list[Path] | None = None) -> dict:
    baseline = _read(before)
    fixed = _read(after)
    if set(baseline) != set(fixed) or len(baseline) != 15:
        raise ValueError("Expected the same 15 selected inputs")
    if any(row.get("category_argument") != "omitted" for row in [*baseline.values(), *fixed.values()]):
        raise ValueError("Name-only comparison contains an explicit category")
    for path in after_updates or ():
        update = _read(path)
        if not set(update) <= set(fixed) or any(row.get("category_argument") != "omitted" for row in update.values()):
            raise ValueError("Targeted update is outside the name-only selected set")
        fixed.update(update)
    guided_latest: dict[int, dict] = {}
    for path in guided:
        guided_latest.update(_read(path))
    if set(guided_latest) != set(baseline):
        raise ValueError("Guided comparison does not contain the same selected inputs")
    records = []
    for index in sorted(baseline):
        old, new, hint = baseline[index], fixed[index], guided_latest[index]
        if old["input"] != new["input"] or old["input"] != hint["input"]:
            raise ValueError(f"Input string changed for row {index}")
        records.append({
            "index": index, "input": old["input"], "identity_level": old["identity_level"],
            "before_name_only_status": old["status"], "after_name_only_status": new["status"],
            "guided_status": hint["status"],
            "before_name_only_queries": old["queries"], "after_name_only_queries": new["queries"],
            "guided_queries": hint["queries"],
            "before_name_only_seconds": old["seconds"], "after_name_only_seconds": new["seconds"],
            "guided_seconds": hint["seconds"],
            "after_verified_exact_urls": " | ".join(new["verified_exact_urls"]),
            "after_page_fetch_outcomes": json.dumps(new["page_fetch_outcomes"], sort_keys=True),
        })
    with output.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(records[0]))
        writer.writeheader()
        writer.writerows(records)
    levels = {}
    for level in ("sku", "model", "family"):
        rows = [row for row in records if row["identity_level"] == level]
        levels[level] = {
            "rows": len(rows),
            "before_pass": sum(row["before_name_only_status"] == "PASS" for row in rows),
            "after_pass": sum(row["after_name_only_status"] == "PASS" for row in rows),
            "guided_pass": sum(row["guided_status"] == "PASS" for row in rows),
            "before_queries": sum(int(row["before_name_only_queries"]) for row in rows),
            "after_queries": sum(int(row["after_name_only_queries"]) for row in rows),
            "guided_queries": sum(int(row["guided_queries"]) for row in rows),
            "before_seconds": round(sum(float(row["before_name_only_seconds"]) for row in rows), 1),
            "after_seconds": round(sum(float(row["after_name_only_seconds"]) for row in rows), 1),
            "guided_seconds": round(sum(float(row["guided_seconds"]) for row in rows), 1),
        }
    summary = {
        "conditions": {
            "before": "discover_name(name), no product_category, commit 334ad11",
            "after": ("discover_name(name), no product_category, scoped product-page recovery"
                      + ("; latest targeted reruns replace matching rows" if after_updates else "")),
            "guided": "discover_name(name, product_category=PRODUCTS category), older selected live runs",
            "same_selected_input_lines": True,
            "same_live_network_state": False,
        },
        "levels": levels,
        "status_before": dict(Counter(row["before_name_only_status"] for row in records)),
        "status_after": dict(Counter(row["after_name_only_status"] for row in records)),
        "status_guided": dict(Counter(row["guided_status"] for row in records)),
        "queries_before": sum(row["before_name_only_queries"] for row in records),
        "queries_after": sum(row["after_name_only_queries"] for row in records),
        "queries_guided": sum(row["guided_queries"] for row in records),
        "seconds_before": round(sum(float(row["before_name_only_seconds"]) for row in records), 1),
        "seconds_after": round(sum(float(row["after_name_only_seconds"]) for row in records), 1),
        "seconds_guided": round(sum(float(row["guided_seconds"]) for row in records), 1),
    }
    output.with_suffix(".json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--before", type=Path, required=True)
    parser.add_argument("--after", type=Path, required=True)
    parser.add_argument("--guided", type=Path, nargs="+", required=True)
    parser.add_argument("--after-update", type=Path, nargs="*")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(compare(args.before, args.after, args.guided, args.output,
                             after_updates=args.after_update), indent=2))
