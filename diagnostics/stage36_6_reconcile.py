"""Reconcile all archived Stage 36.5 candidates with current search-level rules.

This is intentionally a *screen*, not historical ground truth: the archive
does not contain fetched HTML/PDF bytes. It never converts a fresh web page
into a historical content verdict.
"""

from __future__ import annotations

import argparse
from collections import Counter
import csv
import json
from pathlib import Path
from urllib.parse import urlparse

from core.authority_registry import find_seed
from core.discovery import assess_candidate_relevance
from core.match import candidate_model_match
from services.discovery_debug import _page_role
from diagnostics.stage36_5_baseline import PRODUCTS, load_archive, summary


SOURCE_GROUPS = ("official_pages", "support_pages", "documents", "dealers", "secondary", "rejected")


def reconcile(rows: list[dict]) -> tuple[list[dict], list[dict], dict]:
    products: list[dict] = []
    candidates: list[dict] = []
    for index, old in enumerate(rows, 1):
        candidate_start = len(candidates)
        requested_brand, requested_model, category, level = PRODUCTS[index - 1]
        brand, model = old.get("brand", ""), old.get("model", "")
        inspections = {item.get("url") for item in old.get("page_inspections", ())}
        issues: set[str] = set()
        if brand != requested_brand or model != requested_model:
            issues.add("input_boundary_or_encoding")
        if not old.get("official_pages") and not old.get("support_pages") and not old.get("documents"):
            issues.add("official_source_not_found_within_search")
        if any(item.get("status") in {"blocked", "error", "timeout"} for item in old.get("provider_failures", ())):
            issues.add("provider_or_fetch_failure_recorded")

        for group in SOURCE_GROUPS:
            for item in old.get(group, ()):
                url = str(item.get("url") or "")
                title = str(item.get("title") or "")
                role = _page_role(url, title)
                host = (urlparse(url).hostname or "").lower().removeprefix("www.")
                seed = find_seed(brand, host)
                candidate = {
                    "url": url, "title": title,
                    "model_match": candidate_model_match(model, title, urlparse(url).path),
                    "authority_status": "unknown",
                    "source_type": "other",
                }
                relation, reasons = assess_candidate_relevance(candidate, brand, model)
                old_match = str(item.get("model_match") or "")
                screen = "requires_content"
                if role == "forum" and group in {"official_pages", "support_pages"}:
                    screen = "forum_not_editorial_product"
                    issues.add("page_role")
                elif old_match == "exact" and relation == "likely_variant":
                    screen = (
                        "different_package_named_in_saved_title"
                        if "distinct bundle" in "; ".join(reasons)
                        else "variant_or_region_requires_content"
                    )
                    issues.add("identity_or_filtering")
                elif old_match == "exact" and relation == "reject":
                    screen = (
                        "related_item_named_in_saved_title"
                        if any("accessory" in reason or "compatible" in reason for reason in reasons)
                        else "title_url_reject_requires_review"
                    )
                    issues.add("identity_or_filtering")
                elif old_match == "exact" and relation == "weak":
                    screen = "old_exact_requires_main_content"
                    issues.add("identity_unverified")
                if group in {"official_pages", "support_pages", "documents"} and seed is None:
                    issues.add("authority_not_reconstructible_from_saved_record")
                candidates.append({
                    "index": index, "input": old.get("input", ""), "group": group,
                    "url": url, "title": title[:300], "old_model_match": old_match,
                    "old_authority": item.get("authority", ""), "page_role_screen": role,
                    "current_title_url_relation": relation,
                    "current_title_url_reason": "; ".join(reasons),
                    "current_operator_seed": seed.operator_relation if seed else "unknown",
                    "current_seed_scope": seed.scope if seed else "unknown",
                    "archived_page_inspection": url in inspections,
                    "archived_response_body": False,
                    "review_screen": screen,
                    "historical_manual_verdict": "not_reconstructible",
                })
        current_rows = candidates[candidate_start:]
        if "official_source_not_found_within_search" in issues:
            if any(item["group"] == "secondary" and item["current_title_url_relation"] in {"exact", "weak"} for item in current_rows):
                issues.add("possible_authority_or_ranking")
            else:
                issues.add("discovery_no_viable_saved_candidate")
        if old.get("official_pages") and not inspections:
            issues.add("official_page_not_inspected_or_fetch_blocked")
        products.append({
            "index": index, "input": old.get("input", ""), "parsed_brand": brand,
            "parsed_model": model, "structured_brand": requested_brand,
            "structured_model": requested_model, "category": category,
            "identity_level": level, "old_status": old.get("status"),
            "old_exact_official_found": bool(old.get("exact_official_found")),
            "old_exact_product_pages": sum(item.get("model_match") == "exact" for item in old.get("official_pages", ())),
            "old_exact_support_pages": sum(item.get("model_match") == "exact" for item in old.get("support_pages", ())),
            "old_exact_documents": sum(item.get("model_match") == "exact" for item in old.get("documents", ())),
            "old_pdf_documents": sum(urlparse(item.get("url", "")).path.lower().endswith(".pdf") for item in old.get("documents", ())),
            "old_html_documents": sum(not urlparse(item.get("url", "")).path.lower().endswith(".pdf") for item in old.get("documents", ())),
            "provider_failures": len(old.get("provider_failures", ())),
            "runtime_seconds": old.get("runtime_seconds"),
            "possible_failure_causes": ";".join(sorted(issues)),
            "historical_manual_verdict": "not_reconstructible",
            "content_archive_available": False,
        })
    counts = Counter(item["review_screen"] for item in candidates)
    by_level = {
        level: {
            "queries": sum(item["identity_level"] == level for item in products),
            "automatic_pass": sum(item["identity_level"] == level and item["old_status"] == "PASS" for item in products),
        }
        for level in ("sku", "model", "family")
    }
    return products, candidates, {
        "baseline_automatic": summary(rows),
        "archive_candidate_count": len(candidates),
        "screen_counts": dict(counts),
        "by_input_identity_level": by_level,
        "historical_manual_verdicts_reconstructible": 0,
        "note": "Saved URL/title screens are not content verification or first-party/exact precision.",
    }


def write_csv(path: Path, rows: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--archive", type=Path, default=Path("diagnostics/baselines/stage36_5/raw.zip"))
    parser.add_argument("--output", type=Path, default=Path("diagnostics/baselines/stage36_5/reconciliation"))
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    products, candidates, totals = reconcile(load_archive(args.archive))
    write_csv(args.output / "products.csv", products)
    write_csv(args.output / "candidates.csv", candidates)
    (args.output / "summary.json").write_text(json.dumps(totals, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(totals, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
