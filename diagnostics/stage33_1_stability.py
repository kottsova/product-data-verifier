"""Stage 33.1 stability harness: N independent live discovery runs per product.

Every run receives only the product *name* -- no seed URL, no fixture, no
per-product hardcode reaches discovery.  The ``EXPECTED`` table below is a
test oracle used **only** to score a finished run (did the run surface the
official page the maintainers know exists?); it is never imported by, or
passed into, the discovery code.

Usage:
    python -m diagnostics.stage33_1_stability --runs 3
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import time

from core.discovery import clear_official_domain_cache
from services.discovery_debug import DiscoveryDebugResult, DiscoveryDebugService

PRODUCTS = (
    "Gressel GAF-1825",
    "Dreame G12 Pro HHR32A",
    "DEWALT DCD796P2",
    "Philips Sonicare 9900 Prestige HX9992/12",
)

# name -> (regex the official *page* URL must match, or None,
#          regex an official *document* must match, or None)
EXPECTED: dict[str, tuple[str | None, str | None]] = {
    "Gressel GAF-1825": (r"gressel\.ru/catalog/aerogril/aerogril_gressel_gaf_1825/?$", r"gressel\.ru/upload/.+\.pdf"),
    "Dreame G12 Pro HHR32A": (None, r"HHR32A"),
    "DEWALT DCD796P2": (r"dewalt\.co\.uk/en-gb/product/dcd796p2-gb/", None),
    "Philips Sonicare 9900 Prestige HX9992/12": (
        r"philips\.co\.uk/c-p/HX9992_12/sonicare-9900-prestige-power-toothbrush-with-senseiq", None,
    ),
}


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.casefold()).strip("-")


def score(name: str, result: DiscoveryDebugResult) -> dict[str, object]:
    page_pattern, document_pattern = EXPECTED[name]
    page_ok = None
    if page_pattern:
        page_ok = any(
            re.search(page_pattern, item.url, re.I) and item.model_match == "exact"
            for item in result.official_pages
        )
    document_ok = None
    if document_pattern:
        document_ok = any(
            re.search(document_pattern, f"{doc.url} {doc.title}", re.I) and doc.model_match == "exact"
            for doc in result.documents
        )
    oracle_ok = all(flag for flag in (page_ok, document_ok) if flag is not None)
    return {"page_ok": page_ok, "document_ok": document_ok, "oracle_ok": oracle_ok}


def cell(result: DiscoveryDebugResult, verdict: dict[str, object]) -> str:
    mark = "✓" if verdict["oracle_ok"] else "✗"
    return f"{result.status} {mark} ({result.runtime_seconds:.0f}s)"


def run(products: tuple[str, ...], runs: int, output_dir: Path, pause: float) -> dict[str, object]:
    output_dir.mkdir(parents=True, exist_ok=True)
    summary: dict[str, object] = {}
    for name in products:
        cells: list[str] = []
        statuses: list[str] = []
        oracle: list[bool] = []
        for number in range(1, runs + 1):
            clear_official_domain_cache()  # independent run: no remembered domains
            service = DiscoveryDebugService()
            started = time.monotonic()
            result = service.discover_name(name)
            verdict = score(name, result)
            payload = result.to_dict()
            payload["oracle"] = verdict
            payload["run"] = number
            payload["wall_seconds"] = round(time.monotonic() - started, 3)
            (output_dir / f"{slug(name)}-run{number}.json").write_text(
                json.dumps(payload, ensure_ascii=False, indent=1, default=str), encoding="utf-8",
            )
            cells.append(cell(result, verdict))
            statuses.append(result.status)
            oracle.append(bool(verdict["oracle_ok"]))
            print(f"{name} run {number}: {cells[-1]}", flush=True)
            time.sleep(pause)
        summary[name] = {
            "cells": cells,
            "statuses": statuses,
            "oracle": oracle,
            "stable": len(set(statuses)) == 1 and all(oracle),
        }
    return summary


def markdown_table(summary: dict[str, object]) -> str:
    lines = ["| Product | Run 1 | Run 2 | Run 3 | Stable |", "| ------- | ----- | ----- | ----- | ------ |"]
    for name, item in summary.items():
        cells = list(item["cells"]) + ["-"] * 3  # type: ignore[index]
        lines.append(
            f"| {name} | {cells[0]} | {cells[1]} | {cells[2]} | {'yes' if item['stable'] else 'NO'} |"  # type: ignore[index]
        )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--output-dir", type=Path, default=Path("diagnostics/results/stage33_1"))
    parser.add_argument("--pause", type=float, default=2.0)
    parser.add_argument("--only", help="run a single product name")
    args = parser.parse_args()
    products = (args.only,) if args.only else PRODUCTS
    summary = run(products, args.runs, args.output_dir, args.pause)
    table = markdown_table(summary)
    (args.output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8",
    )
    (args.output_dir / "summary.md").write_text(table + "\n", encoding="utf-8")
    print(table)


if __name__ == "__main__":
    main()
