"""Stage 31.4 read-only trace: what happened to the official source?

Runs the live workflow once (no cache) and prints, per candidate, discovery ->
fetch -> identity -> extraction -> mapping -> validation evidence, so the stage
that dropped official evidence can be identified from data, not guesswork.
"""

from __future__ import annotations

import argparse
from collections import Counter
import json
from urllib.parse import urlparse

from core.workflow import ProductWorkflowRequest, run_product_workflow


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower().removeprefix("www.")


def trace(brand: str, model: str, market: str, budget: float) -> dict[str, object]:
    request = ProductWorkflowRequest.from_parts(
        brand, model, market=market, wall_clock_budget_seconds=budget,
    )
    result = run_product_workflow(request)
    raw_by_src = Counter(a.source_url for a in result.raw_attributes)
    map_by_src = Counter(a.source_url for a in result.mapping.canonical_attributes)
    selected = {c["url"] for c in result.selected_candidates}
    candidates = [
        {
            "url": c["url"], "host": _host(c["url"]), "provider": c.get("discovery_provider"),
            "source_type": c.get("source_type"), "authority": c.get("authority_status"),
            "model_match": c.get("model_match"), "identity_relation": c.get("identity_relation"),
            "relevance": c.get("relevance_relation"), "score": c.get("score"),
            "selected_for_fetch": c["url"] in selected,
        }
        for c in result.discovery.candidates
    ]
    rejected = [
        {"url": c["url"], "relevance": c.get("relevance_relation"),
         "reasons": list(c.get("relevance_reasons") or ())[:3]}
        for c in result.discovery.rejected_candidates
    ]
    fetched = [
        {
            "url": s.get("source_url"), "final_url": s.get("final_url"),
            "status": s.get("status"), "blocked_reason": s.get("blocked_reason"),
            "error": s.get("error"), "source_type": s.get("source_type"),
            "authority": s.get("authority_status"), "identity": s.get("identity_relation"),
            "raw_attrs": raw_by_src.get(s.get("source_url"), 0),
            "mapped_attrs": map_by_src.get(s.get("source_url"), 0),
        }
        for s in result.fetched_sources
    ]
    schema_rows = [
        {"name": a.canonical_name, "value": str(a.value)[:60], "status": a.status,
         "source_host": _host(a.source or ""), "authority": a.authority_status,
         "reason": a.resolution_reason,
         "support": [(_host(e.source), e.source_type, str(e.value)[:30]) for e in a.supporting_sources][:4],
         "conflict": [(_host(e.source), e.source_type, str(e.value)[:30]) for e in a.conflicting_values][:4]}
        for a in result.final_profile.attributes if not a.discovered
    ]
    final = [
        {
            "name": a.canonical_name, "value": str(a.value), "status": a.status,
            "source_host": _host(a.source or ""), "authority": a.authority_status,
            "reason": a.resolution_reason,
            "support_hosts": sorted({_host(e.source) for e in a.supporting_sources}),
            "conflict_hosts": sorted({_host(e.source) for e in a.conflicting_values}),
        }
        for a in result.final_profile.attributes if a.value is not None
    ]
    return {
        "schema_rows": schema_rows, "_result": result, "candidates": candidates, "rejected": rejected, "fetched": fetched,
        "final_attributes": final,
        "attempts": [
            {"provider": a.provider, "status": a.status, "results": a.result_count,
             "official_method": a.discovery_method, "official_failure": a.failure_reason,
             "accepted_official_url": a.accepted_official_url}
            for a in result.discovery.provider_attempts
        ],
    }


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--brand", default="Google")
    p.add_argument("--model", default="Pixel 9 Pro")
    p.add_argument("--market", default="RU")
    p.add_argument("--budget", type=float, default=90.0)
    p.add_argument("--output")
    args = p.parse_args()
    data = trace(args.brand, args.model, args.market, args.budget)
    result = data.pop("_result")
    import pickle
    if args.output:
        with open(args.output + ".pkl", "wb") as fh:
            pickle.dump({"identity": result.identity, "discovery": result.discovery,
                         "fetched": result.fetched_sources,
                         "all_fetched": {
                             str(src.get("source_url")): src
                             for src in (
                                 *result.fetched_sources,
                                 *(s2 for f in (result.targeted_search.fields if result.targeted_search else ())
                                   for q in f.query_results for s2 in q.fetched_sources),
                             )
                         }}, fh)
    text = json.dumps(data, ensure_ascii=False, indent=2, default=str)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(text)
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
