"""Archive a selected live diagnostic without exposing raw provider payloads."""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from diagnostics.import_stage36_5 import sanitize, _contains_sensitive_key, _contains_sensitive_url
from diagnostics.stage36_5_baseline import PRODUCTS, load_archive


def report(source: Path, output: Path) -> dict:
    files = sorted(source.glob("[0-9][0-9].json"))
    if not files or output.exists():
        raise ValueError("Source must contain selected rows and output must be new")
    baseline = load_archive(Path("diagnostics/baselines/stage36_6/raw_sanitized.zip"))
    rows = []
    hashes = {}
    with ZipFile(output, "w", ZIP_DEFLATED, compresslevel=9) as archive:
        for path in files:
            index = int(path.stem)
            row = json.loads(path.read_text(encoding="utf-8"))
            if row.get("index") != index:
                raise ValueError(f"Wrong index in {path}")
            cleaned = sanitize(row)
            if _contains_sensitive_key(cleaned) or _contains_sensitive_url(cleaned):
                raise ValueError(f"Sensitive data remains in {path}")
            archive.writestr(path.name, json.dumps(cleaned, ensure_ascii=False, separators=(",", ":")))
            hashes[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
            old = baseline[index - 1]
            rows.append({
                "index": index, "input": row.get("input"), "identity_level": PRODUCTS[index - 1][3],
                "category_argument": row.get("category_argument", "unrecorded"),
                "prior_status": old.get("status"), "status": row.get("status", "ERROR"),
                "prior_queries": len(old.get("attempted_queries", ())),
                "queries": len(row.get("attempted_queries", ())),
                "prior_seconds": old.get("runtime_seconds"), "seconds": row.get("runtime_seconds"),
                "budget_overrun_seconds": row.get("performance", {}).get("budget_overrun_seconds"),
                "verified_exact_urls": [item["url"] for item in row.get("official_pages", ())
                                        if item.get("model_match") == "exact"],
                "page_fetch_outcomes": dict(Counter(item["status"] for item in row.get("page_fetches", ()))),
            })
    levels = {level: {
        "rows": sum(item["identity_level"] == level for item in rows),
        "pass": sum(item["identity_level"] == level and item["status"] == "PASS" for item in rows),
    } for level in ("sku", "model", "family")}
    result = {
        "comparison": "same selected indices against archived Stage 36.6, not a 50-item rerun",
        "category_argument": (rows[0]["category_argument"] if len({row["category_argument"] for row in rows}) == 1
                              else "mixed"),
        "archive_sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
        "raw_sha256": hashes,
        "rows": rows,
        "levels": levels,
        "prior_queries": sum(item["prior_queries"] for item in rows),
        "queries": sum(item["queries"] for item in rows),
        "prior_seconds": round(sum(float(item["prior_seconds"] or 0) for item in rows), 1),
        "seconds": round(sum(float(item["seconds"] or 0) for item in rows), 1),
    }
    output.with_suffix(".json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = report(args.source, args.output)
    print(json.dumps({key: result[key] for key in ("prior_queries", "queries", "prior_seconds", "seconds", "levels")}, indent=2))


if __name__ == "__main__":
    main()
