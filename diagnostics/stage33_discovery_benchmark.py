"""Ten name-only live discovery requests through the Telegram debug handler.

Product names are benchmark inputs, never URL seeds. The five reference
products are read unchanged from the repository dataset; five cross-category
names are the Stage 33.0 brief. No product verification is invoked.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import time

from bot.handlers import handle_discovery_query
from services.discovery_debug import DiscoveryDebugService


REFERENCE_IDS = (
    "cooktop-bosch-pue611bb5e",
    "smartphone-honor-x8d",
    "sewing-janome-sakura95",
    "airfryer-gressel-gaf1825",
    "wetdry-dreame-g12pro-hhr32a",
)
ADDITIONAL_NAMES = (
    "DEWALT DCD796P2",
    "The Ordinary Niacinamide 10% + Zinc 1%",
    "Logitech MX Master 3S",
    "Dyson V15 Detect",
    "Philips Sonicare 9900 Prestige HX9992/12",
)


def benchmark_names(path: Path) -> list[str]:
    products = json.loads(path.read_text(encoding="utf-8"))["products"]
    reference = {item["id"]: item for item in products}
    names = []
    for identifier in REFERENCE_IDS:
        item = reference[identifier]
        names.append(" ".join(filter(None, (
            item["brand"], item["model"], item.get("article"),
        ))))
    return [*names, *ADDITIONAL_NAMES]


async def benchmark(service: DiscoveryDebugService, names: list[str]) -> list[dict]:
    report: list[dict] = []
    for index, name in enumerate(names, start=1):
        messages: list[str] = []

        async def reply(text: str) -> None:
            messages.append(text)

        started = time.monotonic()
        result = await handle_discovery_query(name, index, service, reply=reply)
        report.append({
            "input": name,
            "result": result.to_dict() if result else None,
            "bot_messages": messages,
            "wall_seconds": round(time.monotonic() - started, 3),
        })
        print(f"{index}/{len(names)} {name}: "
              f"{result.status if result else 'ERROR'} "
              f"{result.runtime_seconds if result else '?'}s", flush=True)
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, default=Path("regression/datasets/mvp_50_v1.json"))
    parser.add_argument("--output", type=Path, default=Path("diagnostics/results/stage33-discovery-live.json"))
    parser.add_argument("--budget", type=float, default=60.0)
    parser.add_argument("--include-pixel-isolation", action="store_true")
    parser.add_argument("--name", help="Run one name-only bot request instead of the ten-product suite")
    args = parser.parse_args()
    names = [args.name] if args.name else benchmark_names(args.dataset)
    if args.include_pixel_isolation and not args.name:
        names.extend(("Google Pixel 9", "Google Pixel 9 Pro"))
    report = asyncio.run(benchmark(DiscoveryDebugService(
        wall_clock_budget_seconds=args.budget,
    ), names))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Saved {args.output}")


if __name__ == "__main__":
    main()
