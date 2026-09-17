"""Small Stage 27 smoke for the SERP-free official discovery path."""

from __future__ import annotations

import argparse
import gzip
import json
import re

import requests

from core.discovery import DirectDomainProbeProvider


DEFAULT_PRODUCTS = (
    ("Samsung", "Galaxy Z Flip6", "US"),
    ("Dell", "XPS 13 9340", "US"),
    ("Miele", "KM 7464 FL", "DE"),
    ("Smeg", "SI2M7953D", "GB"),
    ("PFAFF", "ambition 620", "global"),
)


class TracingSession(requests.Session):
    """Keep a compact, non-body trace of each bounded official-site request."""

    def __init__(self, model: str) -> None:
        super().__init__()
        self.model = model
        self.trace: list[dict[str, object]] = []

    def get(self, url: str, **kwargs: object) -> requests.Response:
        try:
            response = super().get(url, **kwargs)
        except requests.RequestException as error:
            self.trace.append({
                "url": url,
                "error": type(error).__name__,
                "message": str(error),
            })
            raise
        body = response.content
        if body.startswith(b"\x1f\x8b"):
            try:
                body = gzip.decompress(body)
            except (EOFError, OSError):
                pass
        text = body.decode(response.encoding or "utf-8", errors="replace")
        compact_model = re.sub(r"[^a-z0-9]", "", self.model.casefold())
        compact_text = re.sub(r"[^a-z0-9]", "", text.casefold())
        self.trace.append({
            "url": url,
            "status": response.status_code,
            "final_url": str(response.url),
            "content_type": response.headers.get("content-type"),
            "bytes": len(response.content),
            "sitemap_locations": len(re.findall(r"<loc\\b", text, re.IGNORECASE)),
            "declared_sitemaps": sum(
                line.casefold().startswith("sitemap:") for line in text.splitlines()
            ),
            "model_in_body": bool(compact_model and compact_model in compact_text),
            "preview": re.sub(r"\s+", " ", text[:500]),
        })
        return response


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--timeout", type=float, default=12.0)
    parser.add_argument("--brands", help="comma-separated subset of default brands")
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
        session = TracingSession(model)
        provider = DirectDomainProbeProvider(
            market,
            timeout_seconds=args.timeout,
            session=session,
        )
        provider.configure_identity(brand, model)
        results = provider.search_with_timeout(
            f"{brand} {model} specifications",
            args.timeout,
        )
        rows.append({
            "brand": brand,
            "model": model,
            "market": market,
            "requests": provider._request_count,
            "responses": provider.last_raw_result_count,
            "method_requests": provider.last_method_requests,
            "discovery_method": provider.last_discovery_method,
            "candidate_count": provider.last_candidate_count,
            "exact_model_candidate_count": provider.last_exact_model_candidate_count,
            "failure_reason": provider.last_failure_reason,
            "trace": session.trace,
            "results": [
                {"url": item.url, "method": item.snippet}
                for item in results
            ],
        })
    print(json.dumps(rows, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
