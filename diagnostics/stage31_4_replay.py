"""Offline replay of a saved stage31_4 trace pickle through the workflow.

Lets the evidence-priority fix be checked against the exact pages a live run
fetched, without depending on a flaky search provider. Optionally injects an
extra official candidate (fetched live) to model discovery finding it.
"""

from __future__ import annotations

import argparse
import copy
import json
import pickle

from core.discovery import DiscoveryOutcome
from core.fetch import fetch_candidate
from core.workflow import ProductWorkflowRequest, WorkflowServices, run_product_workflow


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("pickle")
    p.add_argument("--inject-official-url")
    p.add_argument("--output")
    args = p.parse_args()
    saved = pickle.load(open(args.pickle, "rb"))
    discovery = saved["discovery"]
    candidates = [copy.deepcopy(c) for c in discovery.candidates]
    pool = dict(saved["all_fetched"])
    if args.inject_official_url:
        template = next(c for c in candidates if c["source_type"] == "manufacturer")
        extra = dict(copy.deepcopy(template), url=args.inject_official_url, score=150)
        candidates.insert(0, extra)
        pool[args.inject_official_url] = fetch_candidate(extra, timeout=30)
    outcome = DiscoveryOutcome(
        candidates, "success", ["replay"], ["replay"], [], discovery.provider_attempts,
        discovery.rejected_candidates,
    )

    def fetch(candidate):
        return copy.deepcopy(pool.get(candidate["url"]) or {
            "source_url": candidate["url"], "final_url": candidate["url"],
            "status": "error", "error": "not in replay pool", "document_type": "html",
        })

    services = WorkflowServices(discover_initial=lambda i, m: outcome, fetch=fetch)
    identity = saved["identity"]
    result = run_product_workflow(
        ProductWorkflowRequest.from_parts(identity.brand, identity.base_model, market="RU",
                                          targeted_search_enabled=False),
        services=services,
    )
    profile = result.final_profile
    rows = [
        {"name": a.canonical_name, "value": str(a.value)[:50], "status": a.status,
         "source": (a.source or "")[:60], "reason": a.resolution_reason,
         "support": [(e.source, str(e.value)[:160]) for e in a.supporting_sources][:3],
         "conflicts": [(e.source, str(e.value)[:160]) for e in a.conflicting_values][:3]}
        for a in profile.attributes if not a.discovered
    ]
    out = {"gate": profile.metadata["official_source_resolution"],
           "images": profile.metadata["product_images"],
           "aux": profile.metadata["auxiliary_links"],
           "source_priority": profile.metadata["source_priority"], "rows": rows}
    text = json.dumps(out, ensure_ascii=False, indent=1)
    if args.output:
        open(args.output, "w", encoding="utf-8").write(text)
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
