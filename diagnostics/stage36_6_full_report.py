"""Archive and measure a complete name-only Stage 36.6 run."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import csv
from datetime import datetime
import json
from pathlib import Path
import shutil
from urllib.parse import urlsplit

from diagnostics.stage36_5_baseline import load_archive
from diagnostics.stage36_6_report import _rows, archive_live, compare


def _write_csv(path: Path, records: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(records[0]))
        writer.writeheader()
        writer.writerows(records)


def _url_key(url: str) -> tuple[str, str]:
    parsed = urlsplit(url)
    return (parsed.hostname or "").lower().removeprefix("www."), parsed.path.rstrip("/").lower()


def report(source: Path, output: Path) -> dict:
    rows = _rows(source / "raw")
    preflight = json.loads((source / "preflight.json").read_text(encoding="utf-8"))
    if any(row.get("category_argument") != "omitted" for row in rows):
        raise ValueError("All 50 rows must omit product_category")
    if any(row["input"] != preflight["inputs"][i - 1]["name"] for i, row in enumerate(rows, 1)):
        raise ValueError("Live inputs differ from preflight")
    old = load_archive(Path("diagnostics/baselines/stage36_5/raw.zip"))
    with Path("diagnostics/baselines/stage36_6/transition_causes.csv").open(
        newline="", encoding="utf-8-sig",
    ) as handle:
        historical_unknown_indices = [
            int(item["index"]) for item in csv.DictReader(handle)
            if item["current_adjudication"] == "unknown"
        ]
    comparison, basic = compare(old, rows)
    archive_live(source / "raw", output, preflight["head"])
    shutil.copyfile(source / "preflight.json", output / "preflight.json")
    for name in ("manual_checks.json", "manual_partial_checks.json", "manual_fail_checks.json"):
        if (source / name).is_file():
            shutil.copyfile(source / name, output / name)
    manual_checks = json.loads((source / "manual_checks.json").read_text(encoding="utf-8"))
    manual_by_source = {(int(item["index"]), _url_key(item["url"])): item for item in manual_checks}

    provider: dict[str, dict] = defaultdict(lambda: {"attempts": 0, "seconds": 0.0, "statuses": Counter()})
    fetch_statuses: Counter[str] = Counter()
    fetch_attempt_statuses: Counter[str] = Counter()
    http_statuses: Counter[str] = Counter()
    records: list[dict] = []
    fetch_records: list[dict] = []
    accepted_sources: list[dict] = []
    for index, row in enumerate(rows, 1):
        attempts = row.get("trace", {}).get("provider_attempts", [])
        for attempt in attempts:
            bucket = provider[attempt.get("provider") or "unknown"]
            bucket["attempts"] += 1
            bucket["seconds"] += float(attempt.get("duration_seconds") or 0)
            bucket["statuses"][attempt.get("status") or "unknown"] += 1
        fetches = row.get("page_fetches", [])
        for group in ("official_pages", "support_pages", "documents"):
            for source_item in row.get(group, []):
                matching_fetch = next((fetch for fetch in fetches if _url_key(source_item.get("url", ""))
                                       in {_url_key(fetch.get("url", "")), _url_key(fetch.get("final_url", ""))}), {})
                manual = manual_by_source.get((index, _url_key(source_item.get("url", ""))), {})
                accepted_sources.append({
                    "index": index, "input": row["input"], "identity_level": row["identity_level"],
                    "group": group, "url": source_item.get("url", ""),
                    "title": source_item.get("title", ""),
                    "claimed_match": source_item.get("model_match", ""),
                    "claimed_authority": source_item.get("authority", ""),
                    "content_identity_verified": source_item.get("content_identity_verified", ""),
                    "identity_evidence": source_item.get("identity_evidence", ""),
                    "operator_relation": source_item.get("operator_relation", ""),
                    "authority_scope": source_item.get("authority_scope", ""),
                    "authority_evidence_url": source_item.get("authority_evidence_url", ""),
                    "authority_evidence_excerpt": source_item.get("authority_evidence_excerpt", ""),
                    "fetch_status": matching_fetch.get("status", ""),
                    "fetch_main_product_relation": matching_fetch.get("main_product_relation", ""),
                    "fetch_scoped_seed_first_party": matching_fetch.get("scoped_seed_first_party", ""),
                    "fetch_js_shell": matching_fetch.get("js_shell", ""),
                    "manual_first_party_verdict": manual.get("first_party_verdict", "pending"),
                    "manual_identity_verdict": manual.get("identity_verdict", "pending"),
                    "manual_review_note": manual.get("note", ""),
                })
        for fetch in fetches:
            observed = fetch.get("status") or "unknown"
            fetch_statuses[observed] += 1
            for attempt in fetch.get("attempts", []) or [{}]:
                if attempt:
                    fetch_attempt_statuses[attempt.get("status") or "unknown"] += 1
                    if attempt.get("http_status"):
                        http_statuses[str(attempt["http_status"])] += 1
                fetch_records.append({
                    "index": index, "input": row["input"], "url": fetch.get("url"),
                    "final_url": fetch.get("final_url"), "fetch_status": observed,
                    "attempt_status": attempt.get("status", ""),
                    "http_status": attempt.get("http_status", ""),
                    "duration_seconds": attempt.get("duration_seconds", fetch.get("duration_seconds")),
                    "main_product_relation": fetch.get("main_product_relation", ""),
                    "main_product_evidence": fetch.get("main_product_evidence", ""),
                    "observed_category": fetch.get("observed_category", ""),
                    "category_evidence": fetch.get("category_evidence", ""),
                    "host_seed_operator": fetch.get("host_seed_operator", ""),
                    "scoped_seed_first_party": fetch.get("scoped_seed_first_party", ""),
                    "js_shell": fetch.get("js_shell", ""),
                })
        exact_pages = [page for page in row.get("official_pages", [])
                       if page.get("model_match") == "exact"]
        unproved = [fetch for fetch in fetches if fetch.get("status") == "loaded" and (
            fetch.get("main_product_relation") != "exact"
            or not fetch.get("scoped_seed_first_party") or fetch.get("js_shell"))]
        candidate_counts = row.get("candidate_counts", {})
        records.append({
            "index": index, "input": row["input"], "identity_level": row["identity_level"],
            "status": row.get("status", "ERROR"), "parsed_brand": row.get("brand", ""),
            "parsed_model": row.get("model", ""),
            "queries": len(row.get("attempted_queries", [])),
            "provider_attempts": len(attempts),
            "provider_outcomes": json.dumps(dict(Counter(a.get("status", "unknown") for a in attempts))),
            "service_seconds": row.get("runtime_seconds", ""),
            "harness_seconds": row.get("harness_elapsed_seconds", ""),
            "over_75_seconds": float(row.get("harness_elapsed_seconds") or 0) > 75,
            "search_status": row.get("search_status", ""),
            "candidate_counts": json.dumps(candidate_counts),
            "accepted_official_pages": len(row.get("official_pages", [])),
            "exact_main_product_pages": len(exact_pages),
            "content_verified_exact_pages": sum(bool(page.get("content_identity_verified")) for page in exact_pages),
            "support_pages": len(row.get("support_pages", [])),
            "documents": len(row.get("documents", [])),
            "exact_support_pages": sum(page.get("model_match") == "exact" for page in row.get("support_pages", [])),
            "exact_documents": sum(doc.get("model_match") == "exact" for doc in row.get("documents", [])),
            "fetch_statuses": json.dumps(dict(Counter(f.get("status", "unknown") for f in fetches))),
            "fetch_http_403": sum(a.get("http_status") == 403 for f in fetches for a in f.get("attempts", [])),
            "fetch_timeout": sum(a.get("status") == "timeout" for f in fetches for a in f.get("attempts", [])),
            "js_shells": sum(bool(f.get("js_shell")) for f in fetches),
            "loaded_source_identity_or_scope_unproved": len(unproved),
            "no_accepted_candidate": not bool(row.get("official") or row.get("dealers") or row.get("secondary")),
            "no_first_party_candidate": not bool(row.get("official_pages") or row.get("support_pages") or row.get("documents")),
            "no_page_candidate_checked": not bool(fetches),
            "load_limited": any(f.get("status") != "loaded" for f in fetches),
            "error": row.get("error", ""),
        })
    _write_csv(output / "product_results.csv", records)
    if accepted_sources:
        _write_csv(output / "accepted_sources_for_audit.csv", accepted_sources)
    if fetch_records:
        _write_csv(output / "page_fetches.csv", fetch_records)
    _write_csv(output / "provider_outcomes.csv", [
        {"provider": name, "attempts": value["attempts"],
         "seconds": round(value["seconds"], 3),
         "statuses": json.dumps(dict(value["statuses"]))}
        for name, value in sorted(provider.items())
    ])
    levels = {level: dict(Counter(row["status"] for row in records if row["identity_level"] == level))
              for level in ("sku", "model", "family")}
    reviewed_party = [item for item in accepted_sources
                      if item["manual_first_party_verdict"] in {"confirmed", "contradicted"}]
    reviewed_exact = [item for item in accepted_sources if item["claimed_match"] == "exact"
                      and item["manual_identity_verdict"] in {"confirmed", "contradicted"}]
    basic["first_party_precision"] = {
        "reviewed": len(reviewed_party),
        "confirmed": sum(item["manual_first_party_verdict"] == "confirmed" for item in reviewed_party),
        "false": sum(item["manual_first_party_verdict"] == "contradicted" for item in reviewed_party),
        "value": (sum(item["manual_first_party_verdict"] == "confirmed" for item in reviewed_party)
                  / len(reviewed_party)) if reviewed_party else None,
    }
    basic["exact_identity_precision"] = {
        "reviewed": len(reviewed_exact),
        "confirmed": sum(item["manual_identity_verdict"] == "confirmed" for item in reviewed_exact),
        "false": sum(item["manual_identity_verdict"] == "contradicted" for item in reviewed_exact),
        "value": (sum(item["manual_identity_verdict"] == "confirmed" for item in reviewed_exact)
                  / len(reviewed_exact)) if reviewed_exact else None,
    }
    basic["precision_note"] = "Independent manual labels apply only to accepted sources in this run."
    summary = {
        "head": preflight["head"], "route": preflight["route"],
        "run_wall_seconds_including_pauses": round((
            datetime.fromisoformat(rows[-1]["completed_at_utc"])
            - datetime.fromisoformat(preflight["recorded_at_utc"])
        ).total_seconds(), 3),
        "identity_levels": levels,
        "statuses": dict(Counter(row["status"] for row in records)),
        "accepted_official_product_pages": sum(row["accepted_official_pages"] for row in records),
        "automatic_exact_main_product_pages": sum(row["exact_main_product_pages"] for row in records),
        "content_verified_exact_main_product_pages": sum(row["content_verified_exact_pages"] for row in records),
        "exact_page_claims_without_content_flag": [
            row["index"] for row in records
            if row["exact_main_product_pages"] != row["content_verified_exact_pages"]
        ],
        "support_pages": sum(row["support_pages"] for row in records),
        "documents": sum(row["documents"] for row in records),
        "exact_support_pages": sum(row["exact_support_pages"] for row in records),
        "exact_documents": sum(row["exact_documents"] for row in records),
        "no_accepted_candidate_indices": [row["index"] for row in records if row["no_accepted_candidate"]],
        "no_first_party_candidate_indices": [row["index"] for row in records if row["no_first_party_candidate"]],
        "no_page_candidate_checked_indices": [row["index"] for row in records if row["no_page_candidate_checked"]],
        "load_limited_indices": [row["index"] for row in records if row["load_limited"]],
        "load_limited_nonpass_indices": [row["index"] for row in records
                                         if row["status"] != "PASS" and row["load_limited"]],
        "identity_or_scope_unproved_indices": [row["index"] for row in records if row["loaded_source_identity_or_scope_unproved"]],
        "over_75_second_indices": [row["index"] for row in records if row["over_75_seconds"]],
        "fetch_statuses": dict(fetch_statuses),
        "fetch_attempt_statuses": dict(fetch_attempt_statuses),
        "http_statuses": dict(http_statuses),
        "javascript_shell_pages": sum(row["js_shells"] for row in records),
        "queries": sum(row["queries"] for row in records),
        "provider_attempts": sum(row["provider_attempts"] for row in records),
        "provider_attempt_seconds": round(sum(value["seconds"] for value in provider.values()), 3),
        "provider_statuses": dict(Counter(a.get("status", "unknown") for row in rows
                                          for a in row.get("trace", {}).get("provider_attempts", []))),
        "service_seconds": round(sum(float(row["service_seconds"] or 0) for row in records), 3),
        "harness_seconds": round(sum(float(row["harness_seconds"] or 0) for row in records), 3),
        "historical_transition_causes_unknown": len(historical_unknown_indices),
        "historical_transition_unknown_indices": historical_unknown_indices,
        "manual_accepted_sources_reviewed": sum(item["manual_first_party_verdict"] != "pending"
                                                 and item["manual_identity_verdict"] != "pending"
                                                 for item in accepted_sources),
        "manual_accepted_sources_total": len(accepted_sources),
        "manually_confirmed_first_party_pages": sum(item["group"] == "official_pages"
                                                    and item["manual_first_party_verdict"] == "confirmed"
                                                    for item in accepted_sources),
        "manually_confirmed_exact_main_product_pages": sum(item["group"] == "official_pages"
                                                             and item["claimed_match"] == "exact"
                                                             and item["manual_first_party_verdict"] == "confirmed"
                                                             and item["manual_identity_verdict"] == "confirmed"
                                                             for item in accepted_sources),
        "manually_confirmed_support_pages": sum(item["group"] == "support_pages"
                                                and item["manual_first_party_verdict"] == "confirmed"
                                                and item["manual_identity_verdict"] == "confirmed"
                                                for item in accepted_sources),
        "manually_confirmed_documents": sum(item["group"] == "documents"
                                            and item["manual_first_party_verdict"] == "confirmed"
                                            and item["manual_identity_verdict"] == "confirmed"
                                            for item in accepted_sources),
        "manual_false_first_party": sum(item["manual_first_party_verdict"] == "contradicted"
                                         for item in accepted_sources),
        "manual_false_exact": sum(item["claimed_match"] == "exact"
                                  and item["manual_identity_verdict"] == "contradicted"
                                  for item in accepted_sources),
        "note": "All accepted sources in this run have independent current-page/operator labels; historical transition causes remain unknown.",
        "prior_comparison": basic,
    }
    (output / "full_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    _write_csv(output / "comparison.csv", comparison)
    table = [
        "# Stage 36.6 full name-only run: all 50 rows", "",
        "The identity level is the archived input contract. Source counts are automatic until the separate manual audit.",
        "", "| # | Archived input | Level | Status | Accepted official pages | Exact main pages | Support | Docs | Queries | Seconds | Page loads |",
        "| ---: | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for row in records:
        table.append(
            f"| {row['index']} | {str(row['input']).replace('|', '&#124;')} | {row['identity_level']} "
            f"| {row['status']} | {row['accepted_official_pages']} | {row['exact_main_product_pages']} "
            f"| {row['support_pages']} | {row['documents']} | {row['queries']} "
            f"| {row['harness_seconds']} | {row['fetch_statuses']} |"
        )
    (output / "all_50.md").write_text("\n".join(table) + "\n", encoding="utf-8")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(report(args.source, args.output), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
