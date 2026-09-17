"""Quantitative retrieval-to-confirmation funnel for live diagnostic artifacts."""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import statistics
from typing import Any, Iterable, Mapping, Sequence

from core.discovery import canonicalize_url
from core.schema import get_attribute_schema


FIRST_PARTY_TYPES = {"manufacturer", "official_document"}
GOOD_QUALITY = {"partial", "verified"}


def _urls(items: Iterable[Mapping[str, Any]], field: str = "url") -> set[str]:
    return {
        url for item in items
        if (url := canonicalize_url(str(item.get(field) or "")))
    }


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


def _expected_names(category: str) -> set[str]:
    return {
        item.canonical_name for item in get_attribute_schema(category) if item.expected
    }


def _source_urls(item: Mapping[str, Any]) -> set[str]:
    return _urls((item,), "url") | _urls((item,), "final_url") | _urls((item,), "source_url")


def _ratio(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def _dominant_failure(row: Mapping[str, Any]) -> str:
    if row["timed_out"]:
        return "process timeout"
    if not row["official_discovered"]:
        return "official discovery"
    if row["exact_official_identified"] and not row["exact_official_selected"]:
        return "exact official selection"
    if row["exact_official_selected"] and not row["fetch_attempted"]:
        return "budget before fetch"
    if row["fetch_attempted"] and not row["fetch_succeeded"]:
        return "provider access/fetch"
    if row["fetch_succeeded"] and not row["raw"]:
        return "extraction"
    if row["raw"] and not row["expected_mapped"]:
        return "raw-to-expected mapping"
    if row["expected_mapped"] and not row["authority_accepted"]:
        return "authority"
    if row["authority_accepted"] and not row["confirmed"]:
        return "validation/corroboration"
    if row["budget_exhausted"]:
        return "budget starvation"
    return "none"


def audit_artifact(artifact: Mapping[str, Any]) -> dict[str, Any]:
    products = {item["id"]: item for item in artifact.get("products", ())}
    rows: list[dict[str, Any]] = []
    for record in artifact.get("records", ()):
        product = products[record["product_id"]]
        trace = record.get("trace")
        if not trace:
            row = {
                "id": record["product_id"], "category": None,
                "official_discovered": False, "exact_official_identified": False,
                "exact_official_selected": False, "fetch_attempted": False,
                "fetch_succeeded": False, "raw": 0, "expected_mapped": 0,
                "authority_accepted": False, "validation_passed": False,
                "confirmed": 0, "unresolved": 0, "conflicts": 0,
                "coverage": 0.0, "quality": None,
                "runtime": float(record.get("runtime_seconds") or 0.0),
                "timed_out": bool(record.get("timed_out")),
                "budget_exhausted": False,
            }
            row["dominant_failure"] = _dominant_failure(row)
            rows.append(row)
            continue

        discovery = trace["discovery"]
        candidates = [
            *discovery.get("accepted_candidates", ()),
            *discovery.get("rejected_candidates", ()),
        ]
        official = [item for item in candidates if _official(item)]
        exact_official = [item for item in official if _exact(item)]
        exact_urls = _urls(exact_official)
        selected = trace["relevance_and_ranking"].get("selected", ())
        verification_selected = [
            item for item in selected
            if _official(item)
            and any(
                "content verification" in str(reason).casefold()
                for reason in item.get("relevance_reasons") or ()
            )
        ]
        selected_exact = [
            item for item in selected
            if canonicalize_url(str(item.get("url") or "")) in exact_urls
        ]
        selected_urls = _urls([*selected_exact, *verification_selected])
        fetches = trace["fetch"].get("sources", ())
        exact_fetches = [
            item for item in fetches
            if _source_urls(item) & selected_urls
        ]
        successful = [item for item in exact_fetches if item.get("status") == "success"]
        content_verified = [
            item for item in successful
            if item.get("content_identity_verified")
            or (
                item.get("authority_status") == "verified"
                and item.get("source_type") in FIRST_PARTY_TYPES
                and item.get("identity_relation") in {"same_base_model", "exact_variant"}
                and item.get("model_relevance") == "exact_base_model"
            )
        ]
        if content_verified and not exact_official:
            exact_official = content_verified
        if content_verified and not selected_exact:
            selected_exact = verification_selected
        successful_urls: set[str] = set()
        for item in successful:
            successful_urls.update(_source_urls(item))
        extraction = trace["extraction"]
        official_raw = [
            item for item in extraction.get("attributes", ())
            if canonicalize_url(str(item.get("source_url") or "")) in successful_urls
        ]
        category = trace["category_and_schema"]["category"]
        expected = _expected_names(category)
        mapped = [
            *trace["mapping"].get("mapped", ()),
            *trace["mapping"].get("derived", ()),
        ]
        expected_mapped = [
            item for item in mapped
            if item.get("canonical_name") in expected
            and canonicalize_url(str(item.get("source_url") or "")) in successful_urls
        ]
        facts = [
            item for item in trace["validation"].get("facts", ())
            if item.get("canonical_name") in expected
        ]
        quality = trace["quality"]
        authority_accepted = any(
            item.get("authority_status") == "verified"
            and item.get("identity_relation") in {"same_base_model", "exact_variant"}
            for item in facts
        )
        row = {
            "id": record["product_id"],
            "category": category,
            "expected_category": product.get("expected_category"),
            "official_discovered": bool(official),
            "exact_official_identified": bool(exact_official),
            "exact_official_selected": bool(selected_exact),
            "fetch_attempted": bool(exact_fetches),
            "fetch_succeeded": bool(successful),
            "raw": len(official_raw),
            "expected_mapped": len({item["canonical_name"] for item in expected_mapped}),
            "authority_accepted": authority_accepted,
            "validation_passed": int(quality["confirmed_count"]) > 0,
            "confirmed": int(quality["confirmed_count"]),
            "unresolved": int(quality["unresolved_count"]),
            "conflicts": int(quality["conflict_count"]),
            "coverage": float(quality["coverage_percent"]),
            "quality": quality["status"],
            "runtime": float(record.get("runtime_seconds") or 0.0),
            "timed_out": bool(record.get("timed_out")),
            "budget_exhausted": bool(trace.get("budget", {}).get("exhausted")),
        }
        row["dominant_failure"] = _dominant_failure(row)
        rows.append(row)

    steps = (
        ("official_candidate_discovered", "official_discovered"),
        ("exact_official_candidate_identified", "exact_official_identified"),
        ("exact_official_selected_into_fetch_budget", "exact_official_selected"),
        ("fetch_attempted", "fetch_attempted"),
        ("fetch_succeeded", "fetch_succeeded"),
        ("raw_attributes_extracted", "raw"),
        ("expected_schema_fields_matched", "expected_mapped"),
        ("authority_accepted", "authority_accepted"),
        ("corroboration_validation_passed", "validation_passed"),
        ("confirmed_fact_produced", "confirmed"),
    )
    funnel = {
        name: sum(bool(row[field]) for row in rows) for name, field in steps
    }
    exact = [row for row in rows if row["exact_official_identified"]]
    fetched = [row for row in rows if row["fetch_succeeded"]]
    raw = [row for row in rows if row["raw"]]
    expected_mapped = [row for row in rows if row["expected_mapped"]]
    runtimes = [row["runtime"] for row in rows]
    aggregate = {
        "products": len(rows),
        "funnel": funnel,
        "conversion": {
            "exact_official_discovered_to_fetched": _ratio(
                sum(row["fetch_succeeded"] for row in exact), len(exact),
            ),
            "fetched_official_to_raw": _ratio(
                sum(bool(row["raw"]) for row in fetched), len(fetched),
            ),
            "raw_to_expected_mapped": _ratio(
                sum(bool(row["expected_mapped"]) for row in raw), len(raw),
            ),
            "expected_mapped_to_confirmed": _ratio(
                sum(bool(row["confirmed"]) for row in expected_mapped),
                len(expected_mapped),
            ),
        },
        "confirmed_gt_0": sum(bool(row["confirmed"]) for row in rows),
        "confirmed_gt_0_rate": _ratio(sum(bool(row["confirmed"]) for row in rows), len(rows)),
        "partial_or_verified": sum(row["quality"] in GOOD_QUALITY for row in rows),
        "partial_or_verified_rate": _ratio(
            sum(row["quality"] in GOOD_QUALITY for row in rows), len(rows),
        ),
        "exact_official_use_rate_when_available": _ratio(
            sum(row["fetch_succeeded"] for row in exact), len(exact),
        ),
        "median_coverage": statistics.median(row["coverage"] for row in rows) if rows else 0.0,
        "median_runtime": statistics.median(runtimes) if runtimes else 0.0,
        "max_runtime": max(runtimes, default=0.0),
        "failure_classes": dict(Counter(row["dominant_failure"] for row in rows)),
    }
    return {"rows": rows, "aggregate": aggregate}


def audit_path(path: str | Path) -> dict[str, Any]:
    return audit_artifact(json.loads(Path(path).read_text(encoding="utf-8")))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifact")
    parser.add_argument("--output")
    args = parser.parse_args(argv)
    result = audit_path(args.artifact)
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
