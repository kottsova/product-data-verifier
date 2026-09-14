"""Run a provenance-preserving attribute coverage audit against live discovery.

This diagnostic does not alter pipeline behavior, inject product URLs, or add
facts. Results depend on live search and source availability at execution time.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from typing import Iterable

from core.discovery import canonicalize_url
from core.mapping import CanonicalAttribute
from core.schema import get_attribute_schema, resolve_attribute_definition
from core.workflow import ProductWorkflowRequest, ProductWorkflowResult, run_product_workflow


@dataclass(frozen=True, slots=True)
class AuditProduct:
    brand: str
    model: str
    category: str
    article: str | None = None


PRODUCTS = {
    "bosch": AuditProduct("Bosch", "PUE611BB5E", "cooktop"),
    "honor": AuditProduct("HONOR", "X8d", "smartphone"),
    "janome": AuditProduct("Janome", "Sakura 95", "sewing_machine"),
    "gressel": AuditProduct("Gressel", "GAF-1825", "air_fryer"),
    "dreame": AuditProduct("Dreame", "G12 Pro HHR32A", "wet_dry_vacuum"),
}


def _targeted_query_results(result: ProductWorkflowResult) -> Iterable[object]:
    if result.targeted_search is None:
        return ()
    return (
        query_result
        for field in result.targeted_search.fields
        for query_result in field.query_results
    )


def _targeted_attributes(result: ProductWorkflowResult) -> list[CanonicalAttribute]:
    return [
        fact
        for query_result in _targeted_query_results(result)
        for fact in query_result.mapped_candidate_facts
    ]


def _raw_key(item: object) -> tuple[str, ...]:
    return (
        str(getattr(item, "name", "")),
        str(getattr(item, "raw_value", "")),
        str(getattr(item, "source_url", "")),
        str(getattr(item, "evidence", "")),
    )


def _all_raw_attributes(result: ProductWorkflowResult) -> list[object]:
    items = [
        *result.raw_attributes,
        *(
            item
            for query_result in _targeted_query_results(result)
            for item in query_result.extracted_relevant_facts
        ),
    ]
    found: dict[tuple[str, ...], object] = {}
    for item in items:
        found.setdefault(_raw_key(item), item)
    return list(found.values())


def _source_rows(result: ProductWorkflowResult) -> list[dict[str, object]]:
    fetched = [
        *result.fetched_sources,
        *(
            source
            for query_result in _targeted_query_results(result)
            for source in query_result.fetched_sources
        ),
    ]
    rows: dict[tuple[str, str, str, str, str], dict[str, object]] = {}
    for source in fetched:
        url = str(source.get("source_url") or source.get("final_url") or "")
        key = (
            canonicalize_url(url) or url,
            str(source.get("status") or ""),
            str(source.get("source_type") or "unknown"),
            str(source.get("authority_status") or "unknown"),
            str(source.get("identity_relation") or "unknown"),
        )
        rows.setdefault(key, {
            "url": url,
            "status": key[1],
            "source_type": key[2],
            "authority_status": key[3],
            "identity_relation": key[4],
            "error": source.get("error"),
            "blocked_reason": source.get("blocked_reason"),
        })
    return sorted(rows.values(), key=lambda item: str(item["url"]))


def _provider_attempt_row(attempt: object) -> dict[str, object]:
    return {
        "provider": str(getattr(attempt, "provider")),
        "query": str(getattr(attempt, "query")),
        "status": str(getattr(attempt, "status")),
        "result_count": int(getattr(attempt, "result_count")),
        "message": getattr(attempt, "message"),
        "is_fallback": bool(getattr(attempt, "is_fallback")),
    }


def _candidate_row(candidate: object) -> dict[str, object]:
    item = candidate if isinstance(candidate, dict) else {}
    return {
        "url": str(item.get("url") or ""),
        "title": str(item.get("title") or ""),
        "relevance_relation": str(item.get("relevance_relation") or "unknown"),
        "relevance_reasons": list(item.get("relevance_reasons") or ()),
        "model_match": str(item.get("model_match") or "unknown"),
        "identity_relation": str(item.get("identity_relation") or "unknown"),
        "source_type": str(item.get("source_type") or "unknown"),
        "authority_status": str(item.get("authority_status") or "unknown"),
        "score": int(item.get("score") or 0),
    }


def _targeted_field(result: ProductWorkflowResult, canonical_name: str) -> object | None:
    if result.targeted_search is None:
        return None
    return next(
        (
            field for field in result.targeted_search.fields
            if field.gap.canonical_name == canonical_name
        ),
        None,
    )


def _missing_reason(
    result: ProductWorkflowResult,
    expected_category: str,
    canonical_name: str,
) -> dict[str, str]:
    if not result.discovery.candidates:
        if result.discovery.search_status in {"blocked", "error"}:
            return {
                "reason": "source not fetched or blocked",
                "detail": (
                    f"initial discovery status={result.discovery.search_status}; "
                    f"issues={len(result.discovery.issues)}; no candidates"
                ),
            }
        return {
            "reason": "not found in sources",
            "detail": (
                f"initial discovery status={result.discovery.search_status}; "
                "no candidates"
            ),
        }

    if result.category.category_id != expected_category:
        return {
            "reason": "other",
            "detail": (
                f"detected category {result.category.category_id!r} differs from "
                f"audit category {expected_category!r}"
            ),
        }

    ambiguous = [
        item for item in result.mapping.ambiguous
        if canonical_name in item.candidates
    ]
    if ambiguous:
        return {
            "reason": "mapping missed",
            "detail": f"{len(ambiguous)} ambiguous initial raw mapping(s)",
        }

    alias_raw = [
        item for item in result.mapping.unmapped
        if (
            (definition := resolve_attribute_definition(item.name, expected_category))
            and definition.canonical_name == canonical_name
        )
    ]
    if alias_raw:
        return {
            "reason": "mapping missed",
            "detail": f"{len(alias_raw)} relevant initial raw attribute(s) remained unmapped",
        }

    field = _targeted_field(result, canonical_name)
    if field is not None:
        query_results = list(field.query_results)
        extracted = [
            item for query in query_results for item in query.extracted_relevant_facts
        ]
        if extracted:
            return {
                "reason": "mapping missed",
                "detail": (
                    f"targeted search extracted {len(extracted)} relevant raw fact(s) "
                    "without producing this canonical field"
                ),
            }
        fetched = [source for query in query_results for source in query.fetched_sources]
        if any(source.get("status") == "success" for source in fetched):
            return {
                "reason": "extraction missed",
                "detail": "targeted source fetched successfully but exposed no relevant raw fact",
            }
        failed = [source for source in fetched if source.get("status") != "success"]
        if failed or field.search_status in {"blocked", "error"}:
            return {
                "reason": "source not fetched or blocked",
                "detail": (
                    f"targeted status={field.search_status}; failed/blocked fetches={len(failed)}"
                ),
            }
        return {
            "reason": "not found in sources",
            "detail": f"targeted status={field.search_status}; no relevant evidence found",
        }

    if result.fetch_failures and not any(
        source.get("status") == "success" for source in result.fetched_sources
    ):
        return {
            "reason": "source not fetched or blocked",
            "detail": "all selected initial sources failed or were blocked",
        }
    return {
        "reason": "not found in sources",
        "detail": "not present in initial evidence and no targeted field result",
    }


def audit_result(
    product: AuditProduct,
    result: ProductWorkflowResult,
) -> dict[str, object]:
    base_schema = get_attribute_schema(product.category)
    schema_names = [item.canonical_name for item in base_schema]
    schema_name_set = set(schema_names)
    canonical_facts = [
        *result.mapping.canonical_attributes,
        *_targeted_attributes(result),
    ]
    canonical_names = {item.canonical_name for item in canonical_facts}
    schema_found = [name for name in schema_names if name in canonical_names]
    schema_missing = [name for name in schema_names if name not in canonical_names]
    extra = sorted(canonical_names - schema_name_set)
    by_name = result.final_profile.by_name
    found_statuses = {
        name: {
            "status": by_name[name].status if name in by_name else "not_in_final_profile",
            "resolution_reason": (
                by_name[name].resolution_reason if name in by_name else None
            ),
        }
        for name in schema_found
    }
    missing_reasons = {
        name: _missing_reason(result, product.category, name)
        for name in schema_missing
    }
    sources = _source_rows(result)
    conflicts = result.final_profile.conflicts
    schema_found_profiles = [by_name[name] for name in schema_found if name in by_name]
    successful_urls = {
        canonicalize_url(str(item["url"])) or str(item["url"])
        for item in sources
        if item["status"] == "success"
    }
    targeted_status_counts: dict[str, int] = {}
    targeted_issues: list[dict[str, str]] = []
    targeted_provider_attempts: list[dict[str, object]] = []
    if result.targeted_search is not None:
        for field in result.targeted_search.fields:
            targeted_status_counts[field.search_status] = (
                targeted_status_counts.get(field.search_status, 0) + 1
            )
            for query_result in field.query_results:
                targeted_provider_attempts.extend(
                    _provider_attempt_row(item)
                    for item in query_result.provider_attempts
                )
                targeted_issues.extend({
                    "field": field.gap.canonical_name,
                    "provider": issue.provider or "unknown",
                    "status": issue.status,
                    "query": issue.query,
                    "message": issue.message,
                } for issue in query_result.issues)
    return {
        "product": f"{product.brand} {product.model}",
        "expected_category": product.category,
        "detected_category": result.category.category_id,
        "detected_category_confidence": result.category.confidence,
        "discovery_status": result.discovery.search_status,
        "discovery_attempted_queries": list(result.discovery.attempted_queries),
        "discovery_issues": [
            {
                "status": issue.status,
                "provider": issue.provider or "unknown",
                "query": issue.query,
                "message": issue.message,
            }
            for issue in result.discovery.issues
        ],
        "provider_attempts": [
            _provider_attempt_row(item) for item in result.discovery.provider_attempts
        ],
        "candidate_count": len(result.discovery.candidates),
        "candidate_count_before_relevance_gate": (
            len(result.discovery.candidates) + len(result.discovery.rejected_candidates)
        ),
        "candidate_count_after_relevance_gate": len(result.discovery.candidates),
        "rejected_candidate_count": len(result.discovery.rejected_candidates),
        "selected_candidates": [
            _candidate_row(item) for item in result.selected_candidates
        ],
        "rejected_examples": [
            _candidate_row(item) for item in result.discovery.rejected_candidates[:10]
        ],
        "selected_candidate_count": len(result.selected_candidates),
        "successful_fetches": len(successful_urls),
        "sources": sources,
        "initial_raw_attribute_count": len(result.raw_attributes),
        "raw_attribute_count": len(_all_raw_attributes(result)),
        "canonical_found_count": len(canonical_names),
        "schema_total": len(schema_names),
        "schema_found_count": len(schema_found),
        "schema_missing_count": len(schema_missing),
        "coverage_percent": round(100 * len(schema_found) / len(schema_names), 1),
        "schema_found": schema_found,
        "schema_found_validation": found_statuses,
        "schema_missing": schema_missing,
        "missing_reasons": missing_reasons,
        "discovered_extra": extra,
        "confirmed_count": sum(item.status == "Confirmed" for item in schema_found_profiles),
        "unresolved_count": sum(item.status == "Unresolved" for item in schema_found_profiles),
        "conflict_count": sum(item.status == "Conflict" for item in schema_found_profiles),
        "final_profile_status_counts": {
            status: sum(item.status == status for item in result.final_profile.attributes)
            for status in ("Confirmed", "Unresolved", "Conflict")
        },
        "conflicting_alternative_count": sum(
            len(item.conflicting_values) for item in result.final_profile.attributes
        ),
        "conflict_evidence_count": sum(
            len(item.supporting_sources) + len(item.conflicting_values)
            for item in conflicts
        ),
        "targeted_query_count": len(result.targeted_plan.queries),
        "targeted_fetch_count": result.targeted_search.fetch_count if result.targeted_search else 0,
        "targeted_status_counts": targeted_status_counts,
        "targeted_issues": targeted_issues,
        "targeted_provider_attempts": targeted_provider_attempts,
    }


def run_audit(product: AuditProduct, market: str, max_sources: int) -> dict[str, object]:
    request = ProductWorkflowRequest.from_parts(
        product.brand,
        product.model,
        product.article,
        market=market,
        max_initial_sources=max_sources,
        targeted_search_enabled=True,
    )
    return audit_result(product, run_product_workflow(request))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("product", choices=(*PRODUCTS, "all"))
    parser.add_argument("--market", default="global")
    parser.add_argument("--max-sources", type=int, default=5)
    arguments = parser.parse_args()
    selected = PRODUCTS.values() if arguments.product == "all" else (
        PRODUCTS[arguments.product],
    )
    for product in selected:
        print(json.dumps(
            run_audit(product, arguments.market, arguments.max_sources),
            ensure_ascii=True,
            sort_keys=True,
        ), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
