"""Stage 33.2 blind discovery benchmark: 10 products x N cold runs.

Every run receives only ``brand + model`` text.  No seed URL, domain, category
hint or expected result reaches discovery.  ``EXPECTED`` is a post-run oracle
(what the maintainers verified by hand to exist); it only *scores* saved traces
and is never imported by the discovery code.

Usage:
    python -m diagnostics.stage33_2_benchmark --runs 3
    python -m diagnostics.stage33_2_benchmark --rescore      # re-score saved traces
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import time

from bot.discovery_formatters import format_discovery_result
from core.discovery import clear_official_domain_cache
from services.discovery_debug import DiscoveryDebugResult, DiscoveryDebugService

PRODUCTS = (
    "Gressel GAF-1825",
    "Dreame G12 Pro HHR32A",
    "DEWALT DCD796P2",
    "Philips Sonicare 9900 Prestige HX9992/12",
    "Samsung Galaxy S24 SM-S921B",          # smartphone
    "Bosch HBG7741B1",                      # built-in oven
    "Makita DHP484Z",                       # power tool
    "The Ordinary Niacinamide 10% + Zinc 1%",  # skincare
    "DeLonghi EC685M",                      # small appliance
    "Logitech MX Master 3S",                # computer peripheral
)

# name -> (regex an exact official page URL must match | None,
#          regex an exact official document must match | None)
# Filled from hand-verified facts AFTER runs; see STAGE33_2 report.
EXPECTED: dict[str, tuple[str | None, str | None]] = {
    "Gressel GAF-1825": (r"gressel\.ru/catalog/aerogril/aerogril_gressel_gaf_1825/?$", r"gressel\.ru/upload/.+\.pdf"),
    "Dreame G12 Pro HHR32A": (None, r"HHR32A"),
    "DEWALT DCD796P2": (r"dewalt\.co\.uk/en-gb/product/dcd796p2-gb/", None),
    "Philips Sonicare 9900 Prestige HX9992/12": (
        r"philips\.co\.uk/c-p/HX9992_12/sonicare-9900-prestige-power-toothbrush-with-senseiq", None,
    ),
    # Hand-verified after the 30 runs (each URL fetched, SKU present on the page).
    "Samsung Galaxy S24 SM-S921B": (None, None),  # no exact product page carries SM-S921B in URL/title
    "Bosch HBG7741B1": (r"bosch-home\.com/.+/HBG7741B1/?$", None),  # listed in bosch-home.com/de/de/sitemap.xml
    "Makita DHP484Z": (r"makita\.[a-z.]+/.*dhp484z", None),
    "The Ordinary Niacinamide 10% + Zinc 1%": (r"theordinary\.[a-z]+/.*niacinamide-10-zinc-1", None),
    "DeLonghi EC685M": (r"delonghi\.(com|ru)/.*ec685", None),
    "Logitech MX Master 3S": (r"logitech\.com(:443)?/[a-z-]+/(shop/p|products/mice)/mx-master-3s", None),
}


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.casefold()).strip("-")


def score(name: str, payload: dict) -> dict[str, object]:
    page_pattern, doc_pattern = EXPECTED.get(name, (None, None))
    pages = payload.get("official_pages", [])
    docs = payload.get("documents", [])
    page_ok = None if not page_pattern else any(
        re.search(page_pattern, p["url"], re.I) and p["model_match"] == "exact" for p in pages
    )
    doc_ok = None if not doc_pattern else any(
        re.search(doc_pattern, f"{d['url']} {d['title']}", re.I) and d["model_match"] == "exact" for d in docs
    )
    known = [flag for flag in (page_ok, doc_ok) if flag is not None]
    return {
        "page_ok": page_ok, "document_ok": doc_ok,
        "oracle_ok": all(known) if known else None,
    }


def cell(payload: dict) -> str:
    verdict = payload.get("oracle", {})
    mark = {True: "✓", False: "✗", None: "?"}[verdict.get("oracle_ok")]
    return f"{payload['status']} {mark} ({payload['runtime_seconds']:.0f}s)"


def run_all(products: tuple[str, ...], runs: int, output_dir: Path, pause: float) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    for name in products:
        for number in range(1, runs + 1):
            clear_official_domain_cache()  # cold: no remembered official domains
            result: DiscoveryDebugResult = DiscoveryDebugService().discover_name(name)
            payload = result.to_dict()
            payload["run"] = number
            payload["oracle"] = score(name, payload)
            (output_dir / f"{slug(name)}-run{number}.json").write_text(
                json.dumps(payload, ensure_ascii=False, indent=1, default=str), encoding="utf-8",
            )
            if number == 1:
                text = "\n\n".join(format_discovery_result(result))
                (output_dir / f"{slug(name)}-bot.txt").write_text(text, encoding="utf-8")
            print(f"{name} run {number}: {cell(payload)}", flush=True)
            time.sleep(pause)


def summarize(products: tuple[str, ...], output_dir: Path, runs: int) -> str:
    rows = ["| Product | Run 1 | Run 2 | Run 3 | Stable |", "| --- | --- | --- | --- | --- |"]
    perf = ["| Product | run | s | queries | attempts | raw | unique | accepted | rejected | blocked | timeout | circuit_open |",
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for name in products:
        cells, statuses, oracle = [], [], []
        for number in range(1, runs + 1):
            path = output_dir / f"{slug(name)}-run{number}.json"
            if not path.exists():
                continue
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["oracle"] = score(name, payload)
            cells.append(cell(payload))
            statuses.append(payload["status"])
            oracle.append(payload["oracle"]["oracle_ok"])
            p = payload["performance"]
            flag = " ⚠>60s" if p["slow"] == "over_60s" else " ⚠>30s" if p["slow"] == "over_30s" else ""
            perf.append(
                f"| {name} | {number} | {p['runtime_seconds']}{flag} | {p['query_count']} | {p['provider_attempts']} | "
                f"{p['raw_candidates']} | {p['unique_candidates']} | {p['accepted']} | {p['rejected']} | "
                f"{p['blocked']} | {p['timeout']} | {p['circuit_open']} |"
            )
        cells += ["-"] * 3
        stable = len(set(statuses)) == 1 and all(flag is not False for flag in oracle)
        rows.append(f"| {name} | {cells[0]} | {cells[1]} | {cells[2]} | {'yes' if stable else 'NO'} |")
    return "\n".join(rows) + "\n\n" + "\n".join(perf)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--output-dir", type=Path, default=Path("diagnostics/results/stage33_2"))
    parser.add_argument("--pause", type=float, default=2.0)
    parser.add_argument("--only", help="run a single product name")
    parser.add_argument("--rescore", action="store_true")
    args = parser.parse_args()
    products = (args.only,) if args.only else PRODUCTS
    if not args.rescore:
        run_all(products, args.runs, args.output_dir, args.pause)
    table = summarize(products, args.output_dir, args.runs)
    (args.output_dir / "summary.md").write_text(table + "\n", encoding="utf-8")
    print(table)


if __name__ == "__main__":
    main()
