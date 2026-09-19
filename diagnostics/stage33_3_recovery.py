"""Stage 33.3 regression: Bosch/Makita official discovery recovery.

Same blind input as Stage 33.2 (only ``brand + model``).  ``--no-serp`` runs the
official-only chain (direct probe, no search engine at all), which is how the
"Makita without SERP" acceptance run is executed.  The ``EXPECTED`` oracle of
the Stage 33.2 harness only scores finished traces.

Usage:
    python -m diagnostics.stage33_3_recovery
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import time

from bot.discovery_formatters import format_discovery_result
from core.discovery import DirectDomainProbeProvider, clear_official_domain_cache
from diagnostics.stage33_2_benchmark import EXPECTED, score, slug
from services.discovery_debug import DiscoveryDebugService

# (product, cold runs, official-only?)
PLAN: tuple[tuple[str, int, bool], ...] = (
    ("Bosch HBG7741B1", 3, False),
    ("Makita DHP484Z", 3, False),
    ("Makita DHP484Z", 3, True),
    ("Gressel GAF-1825", 1, False),
    ("Philips Sonicare 9900 Prestige HX9992/12", 1, False),
    ("DEWALT DCD796P2", 1, False),
    ("DeLonghi EC685M", 1, False),
    ("Logitech MX Master 3S", 1, False),
    # effect on products that stay PARTIAL / were never targeted
    ("Dreame G12 Pro HHR32A", 1, False),
    ("Samsung Galaxy S24 SM-S921B", 1, False),
    ("The Ordinary Niacinamide 10% + Zinc 1%", 1, False),
)
BOSCH_TARGET = "bosch-home.com/de/de/product/kochen-backen/herde-backoefen/einbaubackoefen/HBG7741B1"


def baseline_requests(name: str, previous: Path) -> str:
    """Direct-probe HTTP requests of the Stage 33.2 runs (from their saved traces)."""
    values: list[int] = []
    for path in sorted(previous.glob(f"{slug(name)}-run*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        values.append(sum(
            int(item["requests"]) for item in payload.get("official_paths", [])
            if str(item["path"]).startswith("direct_domain_probe")
        ))
    return "/".join(str(value) for value in values) or "-"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=Path("diagnostics/results/stage33_3"))
    parser.add_argument("--previous", type=Path, default=Path("diagnostics/results/stage33_2"))
    parser.add_argument("--pause", type=float, default=2.0)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    rows: list[str] = []
    for name, runs, official_only in PLAN:
        tag = "nosERP" if official_only else "full"
        for number in range(1, runs + 1):
            clear_official_domain_cache()
            service = DiscoveryDebugService(
                providers=(lambda: [DirectDomainProbeProvider()]) if official_only else None,
            )
            started = time.monotonic()
            result = service.discover_name(name)
            payload = result.to_dict()
            payload["run"] = number
            payload["mode"] = tag
            payload["oracle"] = score(name, payload)
            payload["bosch_target_found"] = any(BOSCH_TARGET.lower() in p["url"].lower() for p in payload["official_pages"])
            stem = f"{slug(name)}-{tag}-run{number}"
            (args.output_dir / f"{stem}.json").write_text(
                json.dumps(payload, ensure_ascii=False, indent=1, default=str), encoding="utf-8",
            )
            (args.output_dir / f"{stem}-bot.txt").write_text(
                "\n\n".join(format_discovery_result(result)), encoding="utf-8",
            )
            perf = payload["performance"]
            exact_pages = [p["url"] for p in payload["official_pages"] if p["model_match"] == "exact"]
            rows.append(
                f"| {name} | {tag} | {number} | {payload['status']} "
                f"{'✓' if payload['oracle']['oracle_ok'] else '✗' if payload['oracle']['oracle_ok'] is False else '?'} "
                f"| {perf['runtime_seconds']} | {perf['first_official_path'] or '-'} | "
                f"{perf['probe_requests']} | {perf['domain_probes']} | {len(exact_pages)} | "
                f"{'yes' if payload['bosch_target_found'] else '-'} |"
            )
            print(rows[-1], flush=True)
            time.sleep(args.pause)
    header = (
        "| Product | mode | run | status | s | first official path | probe requests | domain probes "
        "| exact pages | de/de target |\n| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"
    )
    table = header + "\n" + "\n".join(rows)
    baseline = "\n\nStage 33.2 direct-probe requests (runs 1/2/3):\n" + "\n".join(
        f"* {name}: {baseline_requests(name, args.previous)}"
        for name in dict.fromkeys(item[0] for item in PLAN)
    )
    (args.output_dir / "summary.md").write_text(table + baseline + "\n", encoding="utf-8")
    print(table + baseline)


if __name__ == "__main__":
    main()
