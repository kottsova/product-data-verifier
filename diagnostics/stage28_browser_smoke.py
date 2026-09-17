"""Stage 28 smoke: HTTP-only official-first vs browser-backed fallback.

Reuses the exact 5 brands/models/markets from the Stage 27 smoke
(``diagnostics/stage27_official_smoke.py``) so the two stages are directly
comparable. SERP providers are never invoked here -- only the two
official-discovery providers run, standalone, per brand.
"""

from __future__ import annotations

import argparse
import json

from core.discovery import BrowserOfficialDiscoveryProvider, DirectDomainProbeProvider
from core.fetch import fetch_source
from diagnostics.stage27_official_smoke import DEFAULT_PRODUCTS


def _run_http(brand: str, model: str, market: str, timeout: float) -> tuple[
    DirectDomainProbeProvider, list,
]:
    provider = DirectDomainProbeProvider(market, timeout_seconds=timeout)
    provider.configure_identity(brand, model)
    results = provider.search_with_timeout(f"{brand} {model} specifications", timeout)
    return provider, results


def _run_browser(brand: str, model: str, market: str, timeout: float) -> tuple[
    BrowserOfficialDiscoveryProvider, list,
]:
    provider = BrowserOfficialDiscoveryProvider(market, timeout_seconds=timeout)
    provider.configure_identity(brand, model)
    try:
        results = provider.search_with_timeout(f"{brand} {model} specifications", timeout)
    finally:
        provider.release_transient_resources()
    return provider, results


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--http-timeout", type=float, default=12.0)
    parser.add_argument("--browser-timeout", type=float, default=25.0)
    parser.add_argument("--brands", help="comma-separated subset of default brands")
    parser.add_argument(
        "--confirm", action="store_true",
        help="also fetch the accepted URL once to check for post-discovery blocking",
    )
    args = parser.parse_args()
    wanted = {
        item.strip().casefold()
        for item in (args.brands or "").split(",")
        if item.strip()
    }
    rows: list[dict[str, object]] = []
    for brand, model, market in DEFAULT_PRODUCTS:
        if wanted and brand.casefold() not in wanted:
            continue
        http_provider, http_results = _run_http(brand, model, market, args.http_timeout)
        browser_provider, browser_results = _run_browser(brand, model, market, args.browser_timeout)

        http_exact = http_provider.last_exact_model_candidate_count > 0
        browser_exact = browser_provider.last_exact_model_candidate_count > 0
        if http_exact:
            exact_url = next((item.url for item in http_results), None)
        elif browser_exact:
            exact_url = next((item.url for item in browser_results), None)
        else:
            exact_url = None

        confirmed = None
        blocking_reason = None
        if args.confirm and exact_url:
            fetch_result = fetch_source(exact_url, timeout=15)
            confirmed = fetch_result["status"] == "success"
            blocking_reason = fetch_result.get("blocked_reason")

        rows.append({
            "brand": brand,
            "model": model,
            "market": market,
            "http_requests": http_provider._request_count,
            "http_discovery_method": http_provider.last_discovery_method,
            "http_exact_model_candidate_count": http_provider.last_exact_model_candidate_count,
            "http_failure_reason": http_provider.last_failure_reason,
            "browser_invoked": browser_provider.last_browser_invoked,
            "browser_pages_opened": browser_provider.last_pages_opened,
            "browser_navigation_seconds": round(browser_provider.last_navigation_seconds, 3),
            "browser_budget_used_seconds": browser_provider.last_browser_budget_used_seconds,
            "browser_discovery_method": browser_provider.last_discovery_method,
            "browser_exact_model_candidate_count": browser_provider.last_exact_model_candidate_count,
            "browser_rendered_candidate_count": browser_provider.last_rendered_candidate_count,
            "browser_xhr_candidate_count": browser_provider.last_xhr_candidate_count,
            "browser_captcha_detected": browser_provider.last_captcha_detected,
            "browser_failure_reason": browser_provider.last_failure_reason,
            "exact_official_found": bool(http_exact or browser_exact),
            "accepted_official_url": exact_url,
            "confirmed": confirmed,
            "blocking_reason": blocking_reason,
        })
    print(json.dumps(rows, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
