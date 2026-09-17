"""Stage 25 controlled fault injection: force providers down, show degrade-not-collapse.

Unlike a live WAF block (external, non-deterministic), this forces a named
provider class to always fail for the duration of one product run, so the
result is reproducible. It proves the pipeline still returns a bounded,
structured result -- and, where possible, still finds evidence -- when one
or several discovery providers are unavailable, without needing an external
outage to actually be happening at test time.
"""

from __future__ import annotations

import argparse
from contextlib import ExitStack
import json
from pathlib import Path
import time
from typing import Any, Sequence
from unittest.mock import patch

from core import discovery as discovery_module
from core.discovery import clear_official_domain_cache
from services.product_verifier import ProductVerifierService, VerifyProductRequest


class InjectedProviderFailure(RuntimeError):
    """Marker exception for a fault-injected provider (not a real network error)."""


def _forced_failure(*_args: object, **_kwargs: object) -> None:
    raise InjectedProviderFailure(
        "Injected fault: provider forced unavailable for controlled testing."
    )


# Each scenario names the provider class(es) to force down. Method names are
# patched defensively (only if present) so this works whether a provider
# exposes `search`, `search_with_timeout`, or both.
SCENARIOS: dict[str, tuple[str, ...]] = {
    "duckduckgo_html_down": ("DuckDuckGoHtmlSearchProvider",),
    "naver_down": ("NaverSearchProvider",),
    "seznam_down": ("SeznamSearchProvider",),
    "duckduckgo_lite_down": ("DuckDuckGoLiteSearchProvider",),
    "bing_down": ("BingSearchProvider",),
    "google_down": ("GoogleSearchSession",),
    "direct_domain_probe_down": ("DirectDomainProbeProvider",),
    "primary_discovery_provider_down": ("DuckDuckGoHtmlSearchProvider",),
    "multi_provider_simultaneous_down": (
        "DuckDuckGoHtmlSearchProvider", "NaverSearchProvider", "DuckDuckGoLiteSearchProvider",
    ),
    "all_serp_providers_down": (
        "DuckDuckGoHtmlSearchProvider", "NaverSearchProvider",
        "SeznamSearchProvider", "DuckDuckGoLiteSearchProvider",
        "BingSearchProvider", "GoogleSearchSession",
    ),
}

DEFAULT_PRODUCTS: list[dict[str, Any]] = [
    {"id": "fault-apple-iphone15", "brand": "Apple", "model": "iPhone 15", "market": "US"},
    {"id": "fault-bosch-pue611bb5e", "brand": "Bosch", "model": "PUE611BB5E", "market": "DE"},
]


def run_scenario(
    name: str,
    class_names: Sequence[str],
    product: dict[str, Any],
    *,
    wall_clock_budget: float,
    max_sources: int,
) -> dict[str, Any]:
    started = time.monotonic()
    # Every scenario-product run must be independent, matching cold-run
    # semantics: core.discovery's module-level official-domain cache is
    # process-wide, and this script (unlike the real per-product-subprocess
    # blind gate) runs every scenario in one long-lived process, so without
    # this reset a domain a provider found in an earlier scenario for the
    # same brand would silently carry over and contaminate a later one.
    clear_official_domain_cache()
    with ExitStack() as stack:
        patched: list[str] = []
        for class_name in class_names:
            cls = getattr(discovery_module, class_name)
            for method_name in ("search", "search_with_timeout"):
                if hasattr(cls, method_name):
                    stack.enter_context(
                        patch.object(cls, method_name, side_effect=_forced_failure)
                    )
                    patched.append(f"{class_name}.{method_name}")
        service = ProductVerifierService(repository=None)
        request = VerifyProductRequest(
            brand=product["brand"], model=product["model"], article=product.get("article"),
            market=product.get("market", "global"), max_sources=max_sources,
            targeted_search_enabled=True, force_refresh=True,
            wall_clock_budget_seconds=wall_clock_budget,
        )
        result = service.verify(request, correlation_id=f"stage25-fault-{name}-{product['id']}")
    runtime = round(time.monotonic() - started, 3)
    quality = result.quality
    return {
        "scenario": name,
        "product_id": product["id"],
        "disabled_providers": list(class_names),
        "patched_methods": patched,
        "runtime_seconds": runtime,
        "service_success": result.success,
        "service_error": result.error.kind if result.error else None,
        "quality_status": quality.status if quality else None,
        "confirmed_count": quality.confirmed_count if quality else None,
        "coverage_percent": quality.coverage_percent if quality else None,
        "schema_found": quality.schema_found if quality else None,
        "discovered_field_count": len(result.discovered),
        # "collapsed" means total retrieval collapse -- no schema field got
        # any raw value at all, i.e. discovery+fetch produced nothing to
        # work with. It deliberately does NOT require confirmed_count > 0:
        # a product can legitimately fetch real evidence yet still confirm
        # nothing due to the separate, pre-existing authority-corroboration
        # gap documented since Stage 18.7-18.9 (e.g. Bosch) -- that is not
        # a provider-resilience regression and Stage 25 must not touch
        # authority.py to "fix" it.
        "collapsed": bool(
            result.success and quality is not None
            and quality.schema_found == 0 and quality.coverage_percent == 0.0
        ),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wall-clock-budget", type=float, default=60.0)
    parser.add_argument("--max-sources", type=int, default=3)
    parser.add_argument("--output", default="diagnostics/results/stage25-fault-injection.json")
    parser.add_argument(
        "--scenarios", help="comma-separated subset of scenario names; default is all",
    )
    args = parser.parse_args(argv)

    scenario_names = (
        [item.strip() for item in args.scenarios.split(",") if item.strip()]
        if args.scenarios else list(SCENARIOS)
    )
    records: list[dict[str, Any]] = []
    for name in scenario_names:
        class_names = SCENARIOS[name]
        for product in DEFAULT_PRODUCTS:
            record = run_scenario(
                name, class_names, product,
                wall_clock_budget=args.wall_clock_budget, max_sources=args.max_sources,
            )
            records.append(record)
            print(json.dumps(record, ensure_ascii=False))

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({"records": records}, ensure_ascii=False, indent=2), encoding="utf-8")
    collapsed = [item for item in records if item["collapsed"] or not item["service_success"]]
    print(f"\n{len(records)} scenario-product runs, {len(collapsed)} collapsed/failed.")
    return 1 if collapsed else 0


if __name__ == "__main__":
    raise SystemExit(main())
