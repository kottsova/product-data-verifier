"""Run archived names through the public ``discover_name(name)`` route (Stage 36.7).

Same inputs and harness as the Stage 36.6 full run; only the new external limit
differs.  The output directory must be new (raw rows are written per product).
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess

from diagnostics.stage36_5_baseline import PRODUCTS, live


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True,
                          encoding="utf-8", errors="replace").stdout.strip()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--indices", help="Comma-separated 1..50; default all 50")
    args = parser.parse_args()
    if args.output.exists():
        parser.error(f"refusing to reuse existing output: {args.output}")
    selected = {int(part) for part in args.indices.split(",")} if args.indices else None
    if selected is not None and not selected <= set(range(1, len(PRODUCTS) + 1)):
        parser.error("--indices must be within 1..50")
    args.output.mkdir(parents=True)
    (args.output / "preflight.json").write_text(json.dumps({
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "head": _git("rev-parse", "HEAD"), "branch": _git("branch", "--show-current"),
        "working_tree_before_run": _git("status", "--short"),
        "route": "DiscoveryDebugService.discover_name(name)", "category_argument": "omitted",
        "indices": sorted(selected) if selected else "all 50",
        "budget_seconds": 75, "hard_stop_grace_seconds": 5,
        "provider_environment": {k: v for k, v in os.environ.items()
                                 if k.startswith("PDV_")},
    }, indent=2) + "\n", encoding="utf-8")
    live(args.output / "raw", selected_indices=selected)


if __name__ == "__main__":
    main()
