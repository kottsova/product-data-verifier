"""Stage 8.3 read-only pipeline coverage gap diagnosis.

For each product and each missing schema attribute this module builds a
diagnostic chain that traces the attribute through the full pipeline:

    source_candidate_exists?
     → source_selected?
       → source_fetched_ok?
         → raw_attr_extracted?
           → canonical_mapped?
             → targeted_searched?
               → targeted_fetched_ok?
                 → targeted_extracted?
                   → targeted_mapped?
                     → in_final_profile?
                       → validation_status

Each missing attribute is classified into one of the following loss classes:

    source_not_discovered  – 0 candidates, discovery blocked/error
    source_rejected        – all candidates rejected by the relevance gate
    source_not_selected    – candidates passed gate but missed the top-N limit
    fetch_blocked          – selected source(s) returned blocked/403/429
    fetch_error            – selected source(s) returned network/timeout error
    extraction_missed      – fetch succeeded, attribute absent from raw output
    mapping_missed         – raw attribute present but unmapped or ambiguous
    validation_unresolved  – canonical mapping produced Unresolved/Conflict
    genuinely_absent       – explicit source evidence says the field is absent
    unknown                – could not be classified

This script does NOT alter pipeline behaviour, inject URLs, add facts, or
modify any pipeline module. It is fully reproducible: the same workflow
request produces the same diagnosis for a given network state.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import re
import unicodedata
from typing import Any, Iterable

from core.discovery import canonicalize_url
from core.schema import get_attribute_schema
from core.workflow import ProductWorkflowRequest, ProductWorkflowResult, run_product_workflow

# Re-use the product registry from the existing audit to keep a single source
# of truth for product definitions.
from diagnostics.attribute_coverage_audit import PRODUCTS, AuditProduct


# ---------------------------------------------------------------------------
# Loss classification
# ---------------------------------------------------------------------------

LOSS_CLASSES = (
    "source_not_discovered",
    "source_rejected",
    "source_not_selected",
    "fetch_blocked",
    "fetch_error",
    "extraction_missed",
    "mapping_missed",
    "validation_unresolved",
    "genuinely_absent",
    "unknown",
)

CHAIN_KEYS = (
    "source_candidate_exists",
    "source_selected",
    "source_fetched_ok",
    "raw_attr_extracted",
    "canonical_mapped",
    "targeted_searched",
    "targeted_fetched_ok",
    "targeted_extracted",
    "targeted_mapped",
    "in_final_profile",
    "validation_status",
)


def _alias_key(value: str) -> str:
    """Normalise a label the same way the mapping module does."""
    text = unicodedata.normalize("NFKC", value or "").casefold()
    return " ".join(re.findall(r"[\w]+", text))


def _schema_aliases(canonical_name: str, category: str) -> frozenset[str]:
    """Return all normalised alias keys for one canonical attribute."""
    for defn in get_attribute_schema(category):
        if defn.canonical_name == canonical_name:
            return frozenset(
                _alias_key(alias)
                for alias in (canonical_name, *defn.aliases)
                if alias
            )
    return frozenset({_alias_key(canonical_name)})


def _raw_has_alias(raw_attributes: Iterable[Any], aliases: frozenset[str]) -> bool:
    """Return True if any raw attribute name normalises to an expected alias."""
    return any(
        _alias_key(getattr(raw, "name", "")) in aliases
        for raw in raw_attributes
    )


def _source_status(source: Any) -> str:
    if isinstance(source, dict):
        return str(source.get("status") or "")
    return str(getattr(source, "status", "") or "")


def _source_blocked_reason(source: Any) -> str:
    if isinstance(source, dict):
        return str(source.get("blocked_reason") or "")
    return str(getattr(source, "blocked_reason", "") or "")


def _any_ok(sources: Iterable[Any]) -> bool:
    return any(_source_status(s) == "success" for s in sources)


def _any_blocked(sources: Iterable[Any]) -> bool:
    return any(
        _source_status(s) == "blocked" or bool(_source_blocked_reason(s))
        for s in sources
    )


def _targeted_field(result: Any, canonical_name: str) -> Any | None:
    if result.targeted_search is None:
        return None
    return next(
        (f for f in result.targeted_search.fields if f.gap.canonical_name == canonical_name),
        None,
    )


def _targeted_sources(field: Any | None) -> list[Any]:
    if field is None:
        return []
    return [source for query in field.query_results for source in query.fetched_sources]


def _targeted_exact_raw(
    field: Any | None,
    aliases: frozenset[str],
) -> bool:
    if field is None:
        return False
    facts = [
        fact
        for query in field.query_results
        for fact in query.extracted_relevant_facts
    ]
    return _raw_has_alias(facts, aliases)


def _targeted_exact_mapped(field: Any | None, canonical_name: str) -> bool:
    if field is None:
        return False
    return any(
        getattr(fact, "canonical_name", None) == canonical_name
        for query in field.query_results
        for fact in query.mapped_candidate_facts
    )


def _explicit_absence_evidence(field: Any | None) -> tuple[str, ...]:
    """Return affirmative absence evidence exposed by a fixture/provider.

    The production targeted-search contract currently does not infer absence.
    Optional read-only metadata lets deterministic fixtures distinguish a
    proved absence from an extractor that simply returned no fact.
    """
    if field is None:
        return ()
    values: list[str] = []
    for item in (field, *field.query_results):
        raw = getattr(item, "absence_evidence", ())
        if isinstance(raw, str):
            raw = (raw,)
        values.extend(str(value) for value in (raw or ()) if str(value).strip())
    return tuple(values)


# ---------------------------------------------------------------------------
# Chain builder
# ---------------------------------------------------------------------------

def build_chain(
    result: Any,
    canonical_name: str,
    category: str,
) -> dict[str, bool | str | None]:
    """Return the 11-step diagnostic chain for a single missing attribute.

    Each value is True/False once a stage can be evaluated, or None when
    the stage is not reachable (e.g. targeted steps when no targeted search
    was configured for this field).
    """
    chain: dict[str, bool | str | None] = {key: None for key in CHAIN_KEYS}
    aliases = _schema_aliases(canonical_name, category)

    # Initial discovery/fetch/extraction path.
    n_accepted = len(result.discovery.candidates)
    n_rejected = len(result.discovery.rejected_candidates)
    chain["source_candidate_exists"] = (n_accepted + n_rejected) > 0
    chain["source_selected"] = len(result.selected_candidates) > 0
    if chain["source_selected"]:
        chain["source_fetched_ok"] = _any_ok(result.fetched_sources)
    if chain["source_fetched_ok"]:
        chain["raw_attr_extracted"] = _raw_has_alias(result.raw_attributes, aliases)
    initial_mapped_names = {
        attribute.canonical_name for attribute in result.mapping.canonical_attributes
    }
    chain["canonical_mapped"] = canonical_name in initial_mapped_names

    # Targeted search is independent of the initial discovery/fetch outcome.
    field = _targeted_field(result, canonical_name)
    chain["targeted_searched"] = field is not None
    if field is not None:
        chain["targeted_fetched_ok"] = _any_ok(_targeted_sources(field))
        chain["targeted_mapped"] = _targeted_exact_mapped(field, canonical_name)
        chain["targeted_extracted"] = (
            _targeted_exact_raw(field, aliases) or bool(chain["targeted_mapped"])
        )

    # Final-profile placeholders are reported as present, but do not count as
    # evidence that the attribute was found.
    profile_entry = result.final_profile.by_name.get(canonical_name)
    chain["in_final_profile"] = profile_entry is not None
    if profile_entry is not None:
        chain["validation_status"] = profile_entry.status

    return chain


# ---------------------------------------------------------------------------
# Loss classifier
# ---------------------------------------------------------------------------

def classify_loss(
    result: Any,
    canonical_name: str,
    category: str,
) -> tuple[str, str, dict[str, bool | str | None]]:
    """Return ``(loss_class, detail, chain)`` for one missing attribute."""
    chain = build_chain(result, canonical_name, category)
    aliases = _schema_aliases(canonical_name, category)
    field = _targeted_field(result, canonical_name)
    profile_entry = result.final_profile.by_name.get(canonical_name)

    # Prefer the furthest attribute-specific evidence reached by either path.
    if chain["canonical_mapped"] or chain["targeted_mapped"]:
        if profile_entry and profile_entry.status in {"Unresolved", "Conflict"}:
            return (
                "validation_unresolved",
                (
                    "canonical evidence reached validation but final_profile "
                    f"status={profile_entry.status}"
                ),
                chain,
            )
        return (
            "unknown",
            "canonical evidence exists and is not an unresolved validation gap",
            chain,
        )

    if chain["raw_attr_extracted"]:
        unmapped_labels = {
            _alias_key(getattr(raw, "name", "")) for raw in result.mapping.unmapped
        }
        ambiguous_labels = {
            _alias_key(getattr(item.raw_attribute, "name", ""))
            for item in result.mapping.ambiguous
        }
        tags: list[str] = []
        if aliases & ambiguous_labels:
            tags.append("ambiguous")
        if aliases & unmapped_labels:
            tags.append("unmapped")
        suffix = f" (mapping result: {'+'.join(tags)})" if tags else ""
        return (
            "mapping_missed",
            f"initial raw attribute matched a schema alias but was not mapped{suffix}",
            chain,
        )

    if chain["targeted_extracted"] and not chain["targeted_mapped"]:
        return (
            "mapping_missed",
            (
                "targeted extraction found an exact field alias but mapping "
                "produced no exact canonical fact"
            ),
            chain,
        )

    # Targeted outcomes are field-specific, so they take precedence over a
    # less-specific initial discovery failure.
    if field is not None:
        targeted_sources = _targeted_sources(field)
        if chain["targeted_fetched_ok"]:
            absence = _explicit_absence_evidence(field)
            if absence:
                return (
                    "genuinely_absent",
                    "explicit absence evidence: " + " | ".join(absence),
                    chain,
                )
            return (
                "extraction_missed",
                (
                    f"targeted fetch succeeded (field_status={field.search_status}) "
                    "but no exact raw alias was extracted; absence is not proved"
                ),
                chain,
            )
        if _any_blocked(targeted_sources) or getattr(field, "search_status", "") == "blocked":
            return (
                "fetch_blocked",
                (
                    f"targeted search ran (field_status={field.search_status}) "
                    "but fetch/discovery was blocked"
                ),
                chain,
            )
        if targeted_sources or getattr(field, "search_status", "") == "error":
            return (
                "fetch_error",
                (
                    f"targeted search ran (field_status={field.search_status}) "
                    "but fetch/discovery errored"
                ),
                chain,
            )

        targeted_candidates = [
            candidate
            for query in field.query_results
            for candidate in getattr(query, "discovery_candidates", ())
        ]
        targeted_rejected = [
            candidate
            for query in field.query_results
            for candidate in getattr(query, "rejected_candidates", ())
        ]
        if targeted_candidates and len(targeted_rejected) >= len(targeted_candidates):
            return (
                "source_rejected",
                "targeted candidates existed but all were rejected by the identity gate",
                chain,
            )
        if targeted_candidates:
            return (
                "source_not_selected",
                "targeted candidates existed but none reached fetch (limit/dedup/selection)",
                chain,
            )

    # ── 1. No candidates discovered ─────────────────────────────────────────
    if not chain["source_candidate_exists"]:
        status = result.discovery.search_status
        n_issues = len(result.discovery.issues)
        return (
            "source_not_discovered",
            (
                f"discovery_status={status}; issues={n_issues}; "
                "0 candidates before or after the relevance gate"
            ),
            chain,
        )

    # ── 2. All candidates rejected by the relevance gate ────────────────────
    if not result.discovery.candidates and result.discovery.rejected_candidates:
        n_rej = len(result.discovery.rejected_candidates)
        return (
            "source_rejected",
            f"{n_rej} candidate(s) rejected by the relevance gate; fetch never attempted",
            chain,
        )

    # ── 3. Candidates survived the gate but were not selected ───────────────
    if not chain["source_selected"]:
        n_cand = len(result.discovery.candidates)
        return (
            "source_not_selected",
            (
                f"{n_cand} relevance-passing candidate(s) not selected "
                "(score/dedup/max_sources limit)"
            ),
            chain,
        )

    # ── 4-5. All fetches failed ─────────────────────────────────────────────
    if not chain["source_fetched_ok"]:
        n_sel = len(result.selected_candidates)
        sources = list(result.fetched_sources)
        if _any_blocked(sources):
            return (
                "fetch_blocked",
                (
                    f"{n_sel} selected source(s); "
                    "fetch blocked (HTTP 403/429 or bot-check)"
                ),
                chain,
            )
        return (
            "fetch_error",
            (
                f"{n_sel} selected source(s); "
                "all fetches failed (network/timeout/404/unsupported)"
            ),
            chain,
        )

    # ── 6. Raw alias present in initial extraction ──────────────────────────
    if chain["raw_attr_extracted"]:
        if chain["canonical_mapped"]:
            # Mapped but still absent from schema_found — check final profile.
            prof = result.final_profile.by_name.get(canonical_name)
            if prof and prof.status in {"Unresolved", "Conflict"}:
                return (
                    "validation_unresolved",
                    (
                        f"attribute mapped but final_profile status={prof.status}; "
                        "evidence insufficient for Confirmed"
                    ),
                    chain,
                )
            return (
                "unknown",
                "attribute appears mapped and canonical yet absent from schema coverage",
                chain,
            )

        # Raw alias exists but did not make it through mapping
        unmapped_labels = {
            _alias_key(getattr(r, "name", "")) for r in result.mapping.unmapped
        }
        ambiguous_labels = {
            _alias_key(getattr(m.raw_attribute, "name", ""))
            for m in result.mapping.ambiguous
        }
        tags: list[str] = []
        if aliases & ambiguous_labels:
            tags.append("ambiguous")
        if aliases & unmapped_labels:
            tags.append("unmapped")
        suffix = f" (mapping result: {'+'.join(tags)})" if tags else ""
        return (
            "mapping_missed",
            f"raw attribute with matching alias present but not mapped{suffix}",
            chain,
        )

    # ── 7+. No relevant raw attribute from initial fetch ────────────────────
    # ── 8. Initial fetch OK, no targeted search, no raw extraction ──────────
    n_ok = sum(1 for s in result.fetched_sources if _source_status(s) == "success")
    return (
        "extraction_missed",
        (
            f"{n_ok} successful initial fetch(es); "
            "attribute aliases not found in extracted raw output"
        ),
        chain,
    )


# ---------------------------------------------------------------------------
# Per-product diagnosis
# ---------------------------------------------------------------------------

def _targeted_mapped_facts(result: Any) -> list[Any]:
    if result.targeted_search is None:
        return []
    return [
        fact
        for field in result.targeted_search.fields
        for qr in field.query_results
        for fact in qr.mapped_candidate_facts
    ]


def _all_fetched_sources(result: Any) -> list[Any]:
    sources = list(result.fetched_sources)
    if result.targeted_search is not None:
        sources.extend(
            s
            for field in result.targeted_search.fields
            for qr in field.query_results
            for s in qr.fetched_sources
        )
    return sources


def _top_sources(result: Any, limit: int = 5) -> list[dict[str, str]]:
    seen: set[str] = set()
    rows: list[dict[str, str]] = []
    sources: list[tuple[Any, str]] = [
        (source, "initial") for source in result.fetched_sources
    ]
    if result.targeted_search is not None:
        sources.extend(
            (source, "targeted_search")
            for field in result.targeted_search.fields
            for query in field.query_results
            for source in query.fetched_sources
        )
    for source, origin in sources:
        url = str(
            (source.get("source_url") or source.get("final_url") or "")
            if isinstance(source, dict)
            else getattr(source, "source_url", "")
        )
        key = canonicalize_url(url) or url
        if key in seen:
            continue
        seen.add(key)

        def _f(field_name: str) -> str:
            val = (
                source.get(field_name, "")
                if isinstance(source, dict)
                else getattr(source, field_name, "")
            )
            return str(val or "unknown")

        rows.append({
            "url": url,
            "origin": origin,
            "status": _f("status"),
            "source_type": _f("source_type"),
            "authority_status": _f("authority_status"),
            "identity_relation": _f("identity_relation"),
        })
        if len(rows) >= limit:
            break
    return rows


def _targeted_counts(result: Any) -> dict[str, int]:
    if result.targeted_search is None:
        return {
            "targeted_queries": 0,
            "targeted_candidates": 0,
            "targeted_rejected": 0,
            "targeted_fetches": 0,
        }
    queries = [
        query
        for field in result.targeted_search.fields
        for query in field.query_results
    ]
    return {
        "targeted_queries": len(queries),
        "targeted_candidates": sum(
            len(getattr(query, "discovery_candidates", ())) for query in queries
        ),
        "targeted_rejected": sum(
            len(getattr(query, "rejected_candidates", ())) for query in queries
        ),
        "targeted_fetches": sum(len(query.fetched_sources) for query in queries),
    }


def _bottleneck(breakdown: dict[str, int]) -> str | None:
    """Return the dominant loss class using LOSS_CLASSES priority for ties."""
    best_cls: str | None = None
    best_count = 0
    for cls in LOSS_CLASSES:
        count = breakdown[cls]
        if count > best_count:
            best_count = count
            best_cls = cls
    return best_cls


def diagnose_result(
    product: AuditProduct,
    result: Any,
) -> dict[str, Any]:
    """Build the full gap diagnosis report for one product."""
    base_schema = get_attribute_schema(product.category)
    schema_names = [item.canonical_name for item in base_schema]

    canonical_facts = [
        *result.mapping.canonical_attributes,
        *_targeted_mapped_facts(result),
    ]
    canonical_names = {item.canonical_name for item in canonical_facts}

    schema_found = [n for n in schema_names if n in canonical_names]
    schema_missing = [n for n in schema_names if n not in canonical_names]

    # Analyse each missing attribute
    missing_attributes: dict[str, dict[str, Any]] = {}
    missing_breakdown: dict[str, int] = {cls: 0 for cls in LOSS_CLASSES}

    for name in schema_missing:
        loss_class, detail, chain = classify_loss(result, name, product.category)
        missing_attributes[name] = {
            "loss_class": loss_class,
            "chain": chain,
            "detail": detail,
        }
        missing_breakdown[loss_class] += 1

    # A mapped value is counted as found for coverage, but it can still fail
    # validation. Keep those gaps separate from genuinely missing mappings.
    unresolved_attributes: dict[str, dict[str, Any]] = {}
    for name in schema_found:
        profile_entry = result.final_profile.by_name.get(name)
        if profile_entry is None or profile_entry.status not in {"Unresolved", "Conflict"}:
            continue
        loss_class, detail, chain = classify_loss(result, name, product.category)
        unresolved_attributes[name] = {
            "loss_class": loss_class,
            "chain": chain,
            "detail": detail,
        }

    breakdown = dict(missing_breakdown)
    for diagnosis in unresolved_attributes.values():
        breakdown[diagnosis["loss_class"]] += 1

    validation_statuses = {"Confirmed": 0, "Unresolved": 0, "Conflict": 0}
    for name in schema_names:
        profile_entry = result.final_profile.by_name.get(name)
        if profile_entry is not None and profile_entry.status in validation_statuses:
            validation_statuses[profile_entry.status] += 1

    # Successful fetch URLs (initial + targeted, deduplicated)
    all_sources = _all_fetched_sources(result)
    successful_urls: set[str] = set()
    for source in all_sources:
        if _source_status(source) == "success":
            raw_url = str(
                (source.get("source_url") or source.get("final_url") or "")
                if isinstance(source, dict)
                else getattr(source, "source_url", "")
            )
            successful_urls.add(canonicalize_url(raw_url) or raw_url)

    return {
        "product": f"{product.brand} {product.model}",
        "expected_category": product.category,
        "detected_category": result.category.category_id,
        "schema_total": len(schema_names),
        "schema_found": len(schema_found),
        "schema_missing": len(schema_missing),
        "coverage_percent": (
            round(100 * len(schema_found) / len(schema_names), 1)
            if schema_names else 0.0
        ),
        "raw_attribute_count": len(result.raw_attributes),
        "canonical_mapped_count": len(canonical_names),
        "successful_fetches": len(successful_urls),
        "top_sources": _top_sources(result),
        "confirmed": validation_statuses["Confirmed"],
        "unresolved": validation_statuses["Unresolved"],
        "conflicts": validation_statuses["Conflict"],
        "validation_status_breakdown": validation_statuses,
        # Discovery funnel
        "discovery_status": result.discovery.search_status,
        "candidates_before_gate": (
            len(result.discovery.candidates) + len(result.discovery.rejected_candidates)
        ),
        "candidates_after_gate": len(result.discovery.candidates),
        "rejected_by_gate": len(result.discovery.rejected_candidates),
        "selected_candidates": len(result.selected_candidates),
        **_targeted_counts(result),
        # Gap analysis
        "schema_found_list": schema_found,
        "schema_missing_list": schema_missing,
        "missing_attributes": missing_attributes,
        "unresolved_attributes": unresolved_attributes,
        "missing_loss_class_breakdown": missing_breakdown,
        "loss_class_breakdown": breakdown,
        "missing_bottleneck": _bottleneck(missing_breakdown),
        "bottleneck": _bottleneck(breakdown),
    }


def run_diagnosis(product: AuditProduct, market: str, max_sources: int) -> dict[str, Any]:
    request = ProductWorkflowRequest.from_parts(
        product.brand,
        product.model,
        product.article,
        market=market,
        max_initial_sources=max_sources,
        targeted_search_enabled=True,
    )
    return diagnose_result(product, run_product_workflow(request))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("product", choices=(*PRODUCTS, "all"))
    parser.add_argument("--market", default="global")
    parser.add_argument("--max-sources", type=int, default=5)
    args = parser.parse_args()

    selected = (
        list(PRODUCTS.values())
        if args.product == "all"
        else [PRODUCTS[args.product]]
    )
    for product in selected:
        print(
            json.dumps(
                run_diagnosis(product, args.market, args.max_sources),
                ensure_ascii=True,
                sort_keys=True,
            ),
            flush=True,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
