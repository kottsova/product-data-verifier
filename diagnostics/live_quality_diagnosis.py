"""Stage 18 read-only, stage-by-stage live quality diagnosis.

This module does not change pipeline behavior.  It invokes the stable service
with no cache repository, retains the internal result through the documented
first-party diagnostic escape hatch, and serializes evidence already produced
by every pipeline stage.  Each product runs in a killable child process via
the Stage 17 bounded scheduler.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
import importlib.metadata
import json
import platform
from pathlib import Path
import sys
import time
from typing import Any, Mapping, Sequence
import uuid

from core.discovery import discover_identity_with_status
from core.identity import IdentityEvidence, resolve_product_identity
from diagnostics.attribute_coverage_audit import AuditProduct, audit_result
from regression.runner import atomic_write_json, run_products, utc_now
from services.product_verifier import ProductVerifierService, VerifyProductRequest


PRODUCTS: dict[str, dict[str, Any]] = {
    "bosch": {
        "id": "cooktop-bosch-pue611bb5e", "brand": "Bosch", "model": "PUE611BB5E",
        "article": None, "market": "DE", "expected_category": "cooktop",
    },
    "honor": {
        "id": "smartphone-honor-x8d", "brand": "HONOR", "model": "X8d",
        "article": None, "market": "RU", "expected_category": "smartphone",
    },
    "janome": {
        "id": "sewing-janome-sakura95", "brand": "Janome", "model": "Sakura 95",
        "article": None, "market": "RU", "expected_category": "sewing_machine",
    },
    "gressel": {
        "id": "airfryer-gressel-gaf1825", "brand": "Gressel", "model": "GAF-1825",
        "article": None, "market": "RU", "expected_category": "air_fryer",
    },
    "dreame": {
        "id": "wetdry-dreame-g12pro-hhr32a", "brand": "Dreame", "model": "G12 Pro",
        "article": "HHR32A", "market": "RU", "expected_category": "wet_dry_vacuum",
    },
}


def _candidate(candidate: Mapping[str, object]) -> dict[str, Any]:
    return {
        "url": candidate.get("url"), "title": candidate.get("title"),
        "snippet": candidate.get("snippet"),
        "score": candidate.get("score"),
        "source_type": candidate.get("source_type"),
        "authority_status": candidate.get("authority_status"),
        "model_match": candidate.get("model_match"),
        "model_relevance": candidate.get("model_relevance"),
        "identity_relation": candidate.get("identity_relation"),
        "relevance_relation": candidate.get("relevance_relation"),
        "relevance_reasons": list(candidate.get("relevance_reasons") or ()),
        "discovery_provider": candidate.get("discovery_provider"),
        "discovery_query": candidate.get("discovery_query"),
        "discovery_rank": candidate.get("discovery_rank"),
        "raw_url": candidate.get("raw_url"),
        "redirect_url": candidate.get("redirect_url"),
        "parse_status": candidate.get("parse_status"),
        "parse_confidence": candidate.get("parse_confidence"),
        "discovery_provenance": list(candidate.get("discovery_provenance") or ()),
    }


def _provider_attempt(item: Any) -> dict[str, Any]:
    return {
        "provider": item.provider, "query": item.query, "status": item.status,
        "result_count": item.result_count, "message": item.message,
        "is_fallback": item.is_fallback,
        "duration_seconds": item.duration_seconds,
        "timeout_seconds": item.timeout_seconds,
        "timed_out": item.timed_out,
        "blocked": item.blocked,
        "parse_failure": item.parse_failure,
        "exception_class": item.exception_class,
        "circuit_open": item.circuit_open,
        "budget_exhausted": item.budget_exhausted,
        "raw_result_count": item.raw_result_count,
        "parsed_result_count": item.parsed_result_count,
        "deduped_result_count": item.deduped_result_count,
        "transport": item.transport,
    }


def _discovery_result_counts(outcome: Any) -> dict[str, int]:
    attempts = [item for item in outcome.provider_attempts if item.status == "success"]
    global_deduped = len(outcome.candidates) + len(outcome.rejected_candidates)
    if not attempts:
        return {
            "raw": global_deduped,
            "parsed": global_deduped,
            "provider_deduped": global_deduped,
            "global_deduped": global_deduped,
            "accepted": len(outcome.candidates),
            "rejected": len(outcome.rejected_candidates),
        }
    return {
        "raw": sum(item.raw_result_count for item in attempts),
        "parsed": sum(item.parsed_result_count for item in attempts),
        "provider_deduped": sum(item.deduped_result_count for item in attempts),
        "global_deduped": global_deduped,
        "accepted": len(outcome.candidates),
        "rejected": len(outcome.rejected_candidates),
    }


def _source(source: Mapping[str, object], raw_counts: Mapping[str, int]) -> dict[str, Any]:
    url = str(source.get("source_url") or source.get("final_url") or "")
    return {
        "url": url, "final_url": source.get("final_url"),
        "status": source.get("status"), "http_status": source.get("http_status"),
        "fetch_method": source.get("fetch_method"),
        "document_type": source.get("document_type"),
        "content_type": source.get("content_type"),
        "text_status": source.get("text_status"),
        "blocked_reason": source.get("blocked_reason"), "error": source.get("error"),
        "source_type": source.get("source_type"),
        "authority_status": source.get("authority_status"),
        "model_relevance": source.get("model_relevance"),
        "identity_relation": source.get("identity_relation"),
        "content_identity_verified": bool(
            (source.get("discovery_metadata") or {}).get("content_identity_verified")
        ),
        "extracted_attribute_count": raw_counts.get(url, 0),
        "duration_seconds": None,
        "duration_metric_available": False,
    }


def _raw(attribute: Any) -> dict[str, Any]:
    return {
        "name": attribute.name, "value": attribute.value, "unit": attribute.unit,
        "raw_value": attribute.raw_value, "source_url": attribute.source_url,
        "source_type": attribute.source_type, "extraction_method": attribute.extraction_method,
        "confidence": attribute.confidence, "attribute_kind": attribute.attribute_kind,
        "context": attribute.context,
    }


def _mapped(attribute: Any) -> dict[str, Any]:
    return {
        "canonical_name": attribute.canonical_name, "raw_label": attribute.raw_label,
        "raw_value": attribute.raw_value, "source_url": attribute.source_url,
        "mapping_confidence": attribute.mapping_confidence,
        "mapping_reason": attribute.mapping_reason, "derived": attribute.derived,
    }


def _targeted_trace(result: Any) -> dict[str, Any]:
    plan = [{
        "canonical_name": item.canonical_name, "search_intent": item.search_intent,
        "query": item.query, "identity_signals": list(item.identity_signals),
    } for item in result.targeted_plan.queries]
    fields = []
    if result.targeted_search is not None:
        for field in result.targeted_search.fields:
            query_results = []
            for query in field.query_results:
                raw_counts = Counter(item.source_url for item in query.extracted_relevant_facts)
                query_results.append({
                    "query": query.query, "intent": query.query_intent,
                    "discovery_status": query.discovery_status,
                    "provider_attempts": [
                        _provider_attempt(item) for item in query.provider_attempts
                    ],
                    "issues": [{
                        "provider": item.provider, "status": item.status,
                        "query": item.query, "message": item.message,
                    } for item in query.issues],
                    "candidates": [_candidate(item) for item in query.discovery_candidates],
                    "rejected_candidates": [{
                        "reason": item.reason, "candidate": _candidate(item.candidate),
                    } for item in query.rejected_candidates],
                    "fetches": [_source(item, raw_counts) for item in query.fetched_sources],
                    "raw_attributes": [_raw(item) for item in query.extracted_relevant_facts],
                    "mapped_attributes": [_mapped(item) for item in query.mapped_candidate_facts],
                    "related_attributes": [_mapped(item) for item in query.related_candidate_facts],
                    "useful_evidence_found": query.useful_evidence_found,
                    "accepted_candidate_count": query.accepted_candidate_count,
                    "useful_fact_count": query.useful_fact_count,
                    "new_domain_count": query.new_domain_count,
                    "coverage_gain_count": query.coverage_gain_count,
                    "confirmable_fact_gain_count": query.confirmable_fact_gain_count,
                })
            fields.append({
                "canonical_name": field.gap.canonical_name,
                "gap_state": field.gap.gap_state, "gap_reason": field.gap.reason,
                "search_status": field.search_status, "stop_reason": field.stop_reason,
                "useful_evidence_found": field.useful_evidence_found,
                "query_results": query_results,
            })
    return {
        "enabled": result.request.targeted_search_enabled, "plan": plan,
        "deferred_gaps": [item.canonical_name for item in result.targeted_plan.deferred_gaps],
        "fetch_count": result.targeted_search.fetch_count if result.targeted_search else 0,
        "stop_reason": result.targeted_search.stop_reason if result.targeted_search else None,
        "executed_query_count": (
            result.targeted_search.executed_query_count if result.targeted_search else 0
        ),
        "accepted_candidate_count": (
            result.targeted_search.accepted_candidate_count if result.targeted_search else 0
        ),
        "useful_fact_count": (
            result.targeted_search.useful_fact_count if result.targeted_search else 0
        ),
        "discovered_domain_count": (
            result.targeted_search.discovered_domain_count if result.targeted_search else 0
        ),
        "coverage_gain_count": (
            result.targeted_search.coverage_gain_count if result.targeted_search else 0
        ),
        "confirmable_fact_gain_count": (
            result.targeted_search.confirmable_fact_gain_count
            if result.targeted_search else 0
        ),
        "fields": fields,
    }


def build_stage_trace(product: Mapping[str, Any], result: Any) -> dict[str, Any]:
    """Serialize the existing workflow DTOs without recomputing decisions."""
    identity = result.identity
    raw_counts = Counter(item.source_url for item in result.raw_attributes)
    providers: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for attempt in result.discovery.provider_attempts:
        providers[attempt.provider][attempt.status] += attempt.result_count
    validation_authority = Counter(
        fact.authority_status for fact in result.validated_profile.facts
    )
    trace = {
        "input": {
            "brand": product["brand"], "model": product["model"],
            "article": product.get("article"), "market": product.get("market", "global"),
            "max_sources": result.request.max_initial_sources,
            "targeted_search_enabled": result.request.targeted_search_enabled,
            "wall_clock_budget_seconds": result.request.wall_clock_budget_seconds,
        },
        "identity": {
            "brand": identity.brand, "raw_name": identity.raw_name,
            "base_model": identity.base_model, "commercial_model": identity.commercial_model,
            "manufacturer_article": identity.manufacturer_article,
            "product_code": identity.product_code, "sku": identity.sku, "gtin": identity.gtin,
            "variant_suffix": identity.variant_suffix, "market_hint": identity.market_hint,
            "market_scope": identity.market_scope, "confidence": identity.confidence,
            "evidence": list(identity.evidence),
            "candidate_identifiers": list(identity.candidate_identifiers),
        },
        "discovery": {
            "status": result.discovery.search_status,
            "queries": list(result.discovery.queries),
            "attempted_queries": list(result.discovery.attempted_queries),
            "provider_counts": {
                provider: dict(counts) for provider, counts in sorted(providers.items())
            },
            "provider_attempts": [
                _provider_attempt(item) for item in result.discovery.provider_attempts
            ],
            "issues": [{
                "provider": item.provider, "status": item.status,
                "query": item.query, "message": item.message,
            } for item in result.discovery.issues],
            "accepted_candidates": [_candidate(item) for item in result.discovery.candidates],
            "rejected_candidates": [_candidate(item) for item in result.discovery.rejected_candidates],
            "result_counts": _discovery_result_counts(result.discovery),
        },
        "relevance_and_ranking": {
            "before_filter": len(result.discovery.candidates) + len(result.discovery.rejected_candidates),
            "accepted": len(result.discovery.candidates),
            "rejected": len(result.discovery.rejected_candidates),
            "selected": [_candidate(item) for item in result.selected_candidates],
        },
        "fetch": {
            "attempted": len(result.fetched_sources),
            "status_counts": dict(Counter(item.get("status") for item in result.fetched_sources)),
            "sources": [_source(item, raw_counts) for item in result.fetched_sources],
        },
        "extraction": {
            "total": len(result.raw_attributes),
            "method_counts": dict(Counter(item.extraction_method for item in result.raw_attributes)),
            "source_counts": dict(raw_counts),
            "attributes": [_raw(item) for item in result.raw_attributes],
        },
        "category_and_schema": {
            "category": result.category.category_id, "confidence": result.category.confidence,
            "source": result.category.source, "evidence": list(result.category.evidence),
            "expected_category": product["expected_category"],
            "schema_size": len(result.schema),
            "schema": [item.canonical_name for item in result.schema],
        },
        "mapping": {
            "raw_count": len(result.mapping.raw_attributes),
            "mapped_count": len(result.mapping.mapped),
            "derived_count": len(result.mapping.derived),
            "unmapped_count": len(result.mapping.unmapped),
            "ambiguous_count": len(result.mapping.ambiguous),
            "mapped": [_mapped(item) for item in result.mapping.mapped],
            "derived": [_mapped(item) for item in result.mapping.derived],
            "unmapped": [_raw(item) for item in result.mapping.unmapped],
            "ambiguous": [{
                "raw": _raw(item.raw_attribute), "candidates": list(item.candidates),
                "reason": item.reason,
            } for item in result.mapping.ambiguous],
        },
        "gap_detection": {
            "minimum_search_priority": result.gaps.minimum_search_priority,
            "gaps": [{
                "canonical_name": item.canonical_name, "priority": item.priority,
                "state": item.gap_state, "reason": item.reason,
                "targeted_search_allowed": item.targeted_search_allowed,
            } for item in result.gaps.gaps],
        },
        "targeted_search": _targeted_trace(result),
        "validation": {
            "confirmed": len(result.validated_profile.confirmed),
            "unresolved": len(result.validated_profile.unresolved),
            "conflicts": len(result.validated_profile.conflicts),
            "authority_distribution": dict(validation_authority),
            "facts": [{
                "canonical_name": item.canonical_name, "status": item.status,
                "authority_status": item.authority_status,
                "identity_relation": item.identity_relation,
                "confidence": item.confidence, "resolution_reason": item.resolution_reason,
                "supporting_fact_count": len(item.supporting_facts),
                "conflicting_fact_count": len(item.conflicting_facts),
            } for item in result.validated_profile.facts],
            "diagnostics": [{
                "code": item.code, "canonical_name": item.canonical_name,
                "message": item.message,
            } for item in result.validated_profile.diagnostics],
        },
        "quality": result.quality.to_dict(),
        "budget": dict(result.final_profile.metadata.get("wall_clock_budget") or {}),
    }
    trace["coverage_audit"] = audit_result(
        AuditProduct(
            product["brand"], product["model"], product["expected_category"],
            product.get("article"),
        ),
        result,
    )
    return trace


def _worker(product: dict[str, Any], config: dict[str, Any], output_queue: Any) -> None:
    started_at = utc_now()
    started = time.monotonic()
    if config["phase"] == "discovery":
        evidence = ()
        if product.get("article"):
            evidence = (IdentityEvidence(
                "manufacturer_article", product["article"], "application input",
            ),)
        identity = resolve_product_identity(
            f"{product['brand']} {product['model']}",
            brand=product["brand"], evidence=evidence,
        )
        outcome = discover_identity_with_status(identity, product.get("market", "global"))
        elapsed = round(time.monotonic() - started, 6)
        output_queue.put((product["id"], {
            "product_id": product["id"], "started_at": started_at,
            "finished_at": utc_now(), "runtime_seconds": elapsed,
            "timed_out": False, "service_success": True, "service_error": None,
            "cache": {"repository_configured": False, "force_refresh": True},
            "trace": {
                "phase": "discovery",
                "identity": {
                    "brand": identity.brand, "base_model": identity.base_model,
                    "commercial_model": identity.commercial_model,
                    "manufacturer_article": identity.manufacturer_article,
                    "confidence": identity.confidence, "evidence": list(identity.evidence),
                },
                "discovery": {
                    "status": outcome.search_status,
                    "queries": list(outcome.queries),
                    "attempted_queries": list(outcome.attempted_queries),
                    "provider_attempts": [
                        _provider_attempt(item) for item in outcome.provider_attempts
                    ],
                    "issues": [{
                        "provider": item.provider, "status": item.status,
                        "query": item.query, "message": item.message,
                    } for item in outcome.issues],
                    "accepted_candidates": [_candidate(item) for item in outcome.candidates],
                    "rejected_candidates": [_candidate(item) for item in outcome.rejected_candidates],
                    "result_counts": _discovery_result_counts(outcome),
                },
            },
        }))
        return
    request = VerifyProductRequest(
        brand=product["brand"], model=product["model"], article=product.get("article"),
        market=product.get("market", "global"), max_sources=config["max_sources"],
        targeted_search_enabled=config["targeted_search_enabled"], force_refresh=True,
        wall_clock_budget_seconds=config["wall_clock_budget_seconds"],
    )
    service = ProductVerifierService(repository=None)
    service_result, workflow_result = service.verify_with_workflow_result(
        request, correlation_id=f"stage18-diagnosis-{product['id']}",
    )
    elapsed = round(time.monotonic() - started, 6)
    record: dict[str, Any] = {
        "product_id": product["id"], "started_at": started_at,
        "finished_at": utc_now(), "runtime_seconds": elapsed,
        "timed_out": False, "service_success": service_result.success,
        "service_error": service_result.error.to_dict() if service_result.error else None,
        "cache": {"repository_configured": False, "force_refresh": True},
        "trace": build_stage_trace(product, workflow_result) if workflow_result else None,
    }
    output_queue.put((product["id"], record))


def _runtime() -> dict[str, Any]:
    packages = {}
    for name in ("requests", "urllib3", "beautifulsoup4", "playwright", "pypdf"):
        try:
            packages[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            packages[name] = None
    return {
        "python": sys.version, "executable": sys.executable,
        "platform": platform.platform(), "packages": packages,
    }


def _timeout_timestamps(seconds: float) -> tuple[str, str]:
    finished = datetime.now(timezone.utc)
    started = finished - timedelta(seconds=seconds)
    return (
        started.isoformat().replace("+00:00", "Z"),
        finished.isoformat().replace("+00:00", "Z"),
    )


def run(args: argparse.Namespace) -> dict[str, Any]:
    keys = list(PRODUCTS) if args.product == "all" else [args.product]
    if args.product == "all" and (args.model is not None or args.without_article):
        raise ValueError("--model/--without-article require one named product")
    products = []
    for key in keys:
        product = dict(PRODUCTS[key])
        if args.market is not None:
            product["market"] = args.market
        if args.model is not None:
            product["model"] = args.model
        if args.without_article:
            product["article"] = None
        products.append(product)
    config = {
        "concurrency": args.concurrency, "timeout_seconds": args.timeout,
        "max_sources": args.max_sources,
        "targeted_search_enabled": not args.no_targeted_search,
        "wall_clock_budget_seconds": args.wall_clock_budget,
        "phase": args.phase,
    }
    output = Path(args.output)
    artifact: dict[str, Any] = {
        "schema_version": "1.0", "run_id": str(uuid.uuid4()),
        "started_at": utc_now(), "finished_at": None,
        "runtime": _runtime(), "config": config,
        "products": products, "records": [],
    }
    records: dict[str, dict[str, Any]] = {}
    order = {product["id"]: index for index, product in enumerate(products)}

    def save(record: dict[str, Any]) -> None:
        if record.get("timed_out"):
            started_at, finished_at = _timeout_timestamps(args.timeout)
            record.update({
                "started_at": started_at, "finished_at": finished_at,
                "service_success": False, "service_error": {
                    "kind": "timeout", "message": record.get("error_message"),
                },
                "cache": {"repository_configured": False, "force_refresh": True},
                "trace": None,
            })
        records[record["product_id"]] = record
        artifact["records"] = sorted(
            records.values(), key=lambda item: order[item["product_id"]],
        )
        atomic_write_json(output, artifact)

    atomic_write_json(output, artifact)
    run_products(products, config, save, worker_target=_worker)
    artifact["finished_at"] = utc_now()
    atomic_write_json(output, artifact)
    return artifact


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("product", choices=(*PRODUCTS, "all"), default="all", nargs="?")
    parser.add_argument("--market", choices=("DE", "GB", "RU", "US", "global"))
    parser.add_argument("--model", help="single-product controlled input override")
    parser.add_argument("--without-article", action="store_true")
    parser.add_argument("--max-sources", type=int, default=5)
    parser.add_argument("--no-targeted-search", action="store_true")
    parser.add_argument("--phase", choices=("full", "discovery"), default="full")
    parser.add_argument("--timeout", type=float, default=240.0)
    parser.add_argument("--wall-clock-budget", type=float, default=90.0)
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument(
        "--output", default="diagnostics/results/stage18-live-quality.json",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    artifact = run(args)
    print(json.dumps({
        "run_id": artifact["run_id"], "output": args.output,
        "records": [{
            "product_id": item["product_id"], "timed_out": item["timed_out"],
            "runtime_seconds": item["runtime_seconds"],
            "quality": item["trace"].get("quality") if item.get("trace") else None,
        } for item in artifact["records"]],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
