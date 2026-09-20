"""Archive and compare a complete Stage 36.6 live run against Stage 36.5.

The automatic counters below are reachability/decision counters, not manual
first-party or product-identity precision. Unreviewed precision stays null.
"""

from __future__ import annotations

import argparse
from collections import Counter
import csv
import hashlib
import json
from pathlib import Path
from urllib.parse import urlsplit
from zipfile import ZIP_DEFLATED, ZipFile

from diagnostics.import_stage36_5 import sanitize, _contains_sensitive_key, _contains_sensitive_url
from diagnostics.stage36_5_baseline import PRODUCTS, load_archive


def _rows(path: Path) -> list[dict]:
    files = [path / f"{i:02d}.json" for i in range(1, 51)]
    if any(not file.is_file() for file in files):
        raise FileNotFoundError("A complete 01.json through 50.json run is required")
    rows = [json.loads(file.read_text(encoding="utf-8")) for file in files]
    if any(row.get("index") != i or row.get("input_mode") != "historical" for i, row in enumerate(rows, 1)):
        raise ValueError("Rows must be one complete, ordered historical-input run")
    return rows


def _count_exact(items: list[dict]) -> int:
    return sum(item.get("model_match") == "exact" for item in items)


def _by_level(rows: list[dict]) -> dict[str, dict[str, int]]:
    return {
        level: {
            "queries": sum(row["identity_level"] == level for row in rows),
            "automatic_pass": sum(row["identity_level"] == level and row.get("status") == "PASS" for row in rows),
        }
        for level in ("sku", "model", "family")
    }


def compare(old: list[dict], new: list[dict]) -> tuple[list[dict], dict]:
    comparison: list[dict] = []
    for i, (before, after) in enumerate(zip(old, new, strict=True), 1):
        expected_brand, expected_model, _, level = PRODUCTS[i - 1]
        docs = after.get("documents", ())
        comparison.append({
            "index": i, "input": after["input"], "identity_level": level,
            "expected_brand": expected_brand, "expected_model": expected_model,
            "input_boundary_correct": after.get("brand") == expected_brand and after.get("model") == expected_model,
            "old_status": before.get("status"), "new_status": after.get("status", "ERROR"),
            "old_automatic_pass": bool(before.get("exact_official_found")),
            "new_automatic_pass": bool(after.get("exact_official_found")),
            "new_exact_product_pages": _count_exact(after.get("official_pages", ())),
            "new_content_verified_product_pages": sum(bool(item.get("content_identity_verified")) for item in after.get("official_pages", ())),
            "new_exact_support_pages": _count_exact(after.get("support_pages", ())),
            "new_exact_pdf_documents": sum(item.get("model_match") == "exact" and item.get("file_type") == "pdf" for item in docs),
            "new_exact_html_documents": sum(item.get("model_match") == "exact" and item.get("file_type") != "pdf" for item in docs),
            "new_official_pages_total": len(after.get("official_pages", ())),
            "new_dealer_sources": len(after.get("dealers", ())),
            "new_secondary_sources": len(after.get("secondary", ())),
            "new_rejected_sources": len(after.get("rejected", ())),
            "old_queries": len(before.get("attempted_queries", ())),
            "new_queries": len(after.get("attempted_queries", ())),
            "old_provider_failures": len(before.get("provider_failures", ())),
            "new_provider_failures": len(after.get("provider_failures", ())),
            "old_runtime_seconds": before.get("runtime_seconds"),
            "new_runtime_seconds": after.get("runtime_seconds"),
            "new_search_status": after.get("search_status", "error"),
            "new_error": after.get("error", ""),
        })
    totals = {
        "runs": len(new),
        "unchanged_historical_inputs": sum(before.get("input") == after.get("input") for before, after in zip(old, new, strict=True)),
        "old_automatic_pass": sum(row["old_automatic_pass"] for row in comparison),
        "new_automatic_pass": sum(row["new_automatic_pass"] for row in comparison),
        "pass_to_nonpass": [row["index"] for row in comparison if row["old_automatic_pass"] and not row["new_automatic_pass"]],
        "nonpass_to_pass": [row["index"] for row in comparison if not row["old_automatic_pass"] and row["new_automatic_pass"]],
        "new_any_official_or_document": sum(bool(row.get("official") or row.get("documents")) for row in new),
        "new_exact_product_page_records": sum(row["new_exact_product_pages"] for row in comparison),
        "new_exact_support_page_records": sum(row["new_exact_support_pages"] for row in comparison),
        "new_exact_pdf_records": sum(row["new_exact_pdf_documents"] for row in comparison),
        "new_exact_html_document_records": sum(row["new_exact_html_documents"] for row in comparison),
        "new_automatic_pass_by_identity_level": _by_level(new),
        "new_queries": sum(row["new_queries"] for row in comparison),
        "new_provider_failures": sum(row["new_provider_failures"] for row in comparison),
        "new_provider_failure_statuses": dict(Counter(
            failure.get("status", "unknown") for row in new for failure in row.get("provider_failures", ())
        )),
        "new_runtime_seconds": round(sum(float(row.get("runtime_seconds") or 0) for row in new), 1),
        "failed_harness_rows": [row["index"] for row in comparison if row["new_error"]],
        "first_party_precision": {"reviewed": 0, "confirmed": 0, "false": 0, "value": None},
        "exact_identity_precision": {"reviewed": 0, "confirmed": 0, "false": 0, "value": None},
        "precision_note": "Automatic PASS and registry decisions are not independent manual precision labels.",
    }
    return comparison, totals


def archive_live(source: Path, target: Path, code_commit: str) -> None:
    rows = _rows(source)
    target.mkdir(parents=True, exist_ok=False)
    archive_path = target / "raw_sanitized.zip"
    hashes: dict[str, str] = {}
    with ZipFile(archive_path, "w", ZIP_DEFLATED, compresslevel=9) as archive:
        for index, row in enumerate(rows, 1):
            path = source / f"{index:02d}.json"
            hashes[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
            cleaned = sanitize(row)
            if _contains_sensitive_key(cleaned) or _contains_sensitive_url(cleaned):
                raise ValueError(f"Sensitive data remains in {path.name}")
            archive.writestr(path.name, json.dumps(cleaned, ensure_ascii=False, separators=(",", ":")))
    manifest = {
        "code_commit": code_commit,
        "input_mode": "historical",
        "market": "global",
        "wall_clock_budget_seconds": 75,
        "per_row_service": "new DiscoveryDebugService instance",
        "explicit_cache_reset": "clear_official_domain_cache only",
        "provider_health_reset": False,
        "pause_between_rows_seconds": 2,
        "source_original_sha256": hashes,
        "sanitized_archive_sha256": hashlib.sha256(archive_path.read_bytes()).hexdigest(),
        "sanitization": "credential-like JSON keys and URL query values redacted",
    }
    (target / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


def audit_live(target: Path) -> None:
    manifest = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
    archive_path = target / "raw_sanitized.zip"
    if hashlib.sha256(archive_path.read_bytes()).hexdigest() != manifest["sanitized_archive_sha256"]:
        raise ValueError("Sanitized archive SHA-256 mismatch")
    with ZipFile(archive_path) as archive:
        if archive.namelist() != [f"{i:02d}.json" for i in range(1, 51)]:
            raise ValueError("Archive does not contain ordered 01..50.json")
        for index, name in enumerate(archive.namelist(), 1):
            row = json.loads(archive.read(name))
            if row.get("index") != index or row.get("input_mode") != "historical":
                raise ValueError(f"Wrong index or input mode in {name}")
            if _contains_sensitive_key(row) or _contains_sensitive_url(row):
                raise ValueError(f"Sensitive key or URL query in {name}")
        manual_path = target / "manual_exact_audit.csv"
        if manual_path.exists():
            with manual_path.open(newline="", encoding="utf-8") as handle:
                reviewed = list(csv.DictReader(handle))
            if len(reviewed) != 10 or len({int(item["index"]) for item in reviewed}) != 10:
                raise ValueError("Manual accepted-page audit must have ten unique rows")
            def host_path(url: str) -> tuple[str, str]:
                parsed = urlsplit(url)
                return parsed.hostname.removeprefix("www.").lower(), parsed.path.rstrip("/").lower()
            for item in reviewed:
                index = int(item["index"])
                live_row = json.loads(archive.read(f"{index:02d}.json"))
                exact_urls = {
                    host_path(page["url"]) for page in live_row.get("official_pages", ())
                    if page.get("model_match") == "exact"
                }
                if host_path(item["product_url"]) not in exact_urls:
                    raise ValueError(f"Manual audit URL is not an accepted exact URL in row {index}")
                if item["identity_verdict"] not in {"confirmed", "contradicted", "unknown"}:
                    raise ValueError(f"Invalid identity verdict in row {index}")
                if item["first_party_verdict"] not in {"confirmed", "contradicted", "unknown"}:
                    raise ValueError(f"Invalid first-party verdict in row {index}")
    structured_manifest = target / "structured_manifest.json"
    if structured_manifest.exists():
        details = json.loads(structured_manifest.read_text(encoding="utf-8"))
        structured_archive = target / "structured_corrections.zip"
        if hashlib.sha256(structured_archive.read_bytes()).hexdigest() != details["archive_sha256"]:
            raise ValueError("Structured correction archive SHA-256 mismatch")
        with ZipFile(structured_archive) as archive:
            if archive.namelist() != ["11.json", "48.json"]:
                raise ValueError("Structured archive must contain only rows 11 and 48")
            for name in archive.namelist():
                row = json.loads(archive.read(name))
                if row.get("index") != int(name[:2]) or row.get("input_mode") != "structured":
                    raise ValueError(f"Invalid structured row {name}")
                if _contains_sensitive_key(row) or _contains_sensitive_url(row):
                    raise ValueError(f"Sensitive data in structured row {name}")


def archive_structured(source: Path, target: Path, code_commit: str) -> dict:
    archive_path = target / "structured_corrections.zip"
    manifest_path = target / "structured_manifest.json"
    if archive_path.exists() or manifest_path.exists():
        raise FileExistsError("Refusing to overwrite structured correction results")
    old = load_archive(Path("diagnostics/baselines/stage36_5/raw.zip"))
    records = []
    hashes = {}
    with ZipFile(archive_path, "w", ZIP_DEFLATED, compresslevel=9) as archive:
        for index in (11, 48):
            path = source / f"{index:02d}.json"
            row = json.loads(path.read_text(encoding="utf-8"))
            if row.get("index") != index or row.get("input_mode") != "structured":
                raise ValueError(f"Wrong structured input at {path}")
            hashes[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
            cleaned = sanitize(row)
            if _contains_sensitive_key(cleaned) or _contains_sensitive_url(cleaned):
                raise ValueError(f"Sensitive data remains in {path.name}")
            archive.writestr(path.name, json.dumps(cleaned, ensure_ascii=False, separators=(",", ":")))
            records.append({
                "index": index, "input": row["input"], "parsed_brand": row.get("brand"),
                "parsed_model": row.get("model"), "status": row.get("status"),
                "historical_status": old[index - 1].get("status"),
                "provider_failures": len(row.get("provider_failures", ())),
                "runtime_seconds": row.get("runtime_seconds"),
            })
    manifest_path.write_text(json.dumps({
        "code_commit": code_commit, "input_mode": "structured", "indices": [11, 48],
        "same_sequential_provider_conditions_as_full_run": False,
        "original_files_sha256": hashes,
        "archive_sha256": hashlib.sha256(archive_path.read_bytes()).hexdigest(),
    }, indent=2) + "\n", encoding="utf-8")
    summary = {"rows": records, "warning": "Separate input correction run; do not add to the 50 historical-query denominator."}
    (target / "structured_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live-dir", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--code-commit")
    parser.add_argument("--audit", action="store_true")
    parser.add_argument("--structured-dir", type=Path)
    args = parser.parse_args()
    if args.audit:
        audit_live(args.output)
        print("Stage 36.6 archive audit OK")
        return
    if args.structured_dir is not None:
        if args.code_commit is None:
            parser.error("structured archive requires --code-commit")
        print(json.dumps(archive_structured(args.structured_dir, args.output, args.code_commit), ensure_ascii=False, indent=2))
        return
    if args.live_dir is None or args.code_commit is None:
        parser.error("comparison requires --live-dir and --code-commit")
    old = load_archive(Path("diagnostics/baselines/stage36_5/raw.zip"))
    new = _rows(args.live_dir)
    comparison, totals = compare(old, new)
    archive_live(args.live_dir, args.output, args.code_commit)
    with (args.output / "comparison.csv").open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(comparison[0]))
        writer.writeheader()
        writer.writerows(comparison)
    (args.output / "summary.json").write_text(json.dumps(totals, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(totals, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
