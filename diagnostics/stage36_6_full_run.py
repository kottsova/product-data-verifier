"""Preflight and run all archived names through the public discovery route.

The output directory must be new. Each result is written by the historical
harness immediately after its product finishes; an interrupted run is retained.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess

from core import discovery
from core.discovery import DiscoveryRuntimeConfig, ResilientSearchSession
from core.provider_health import ProviderHealthStore
from diagnostics.stage36_5_baseline import PRODUCTS, live, load_archive


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], check=True, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    ).stdout.strip()


def preflight() -> dict:
    historical_path = Path("diagnostics/baselines/stage36_5/raw.zip")
    historical = load_archive(historical_path)
    if len(historical) != len(PRODUCTS) or any(not row.get("input") for row in historical):
        raise ValueError("Archived 50 historical names are incomplete")
    config = DiscoveryRuntimeConfig()
    session = ResilientSearchSession("global", config=config)
    health = ProviderHealthStore.from_env()
    health_path = os.environ.get("PDV_PROVIDER_HEALTH_PATH")
    health_file = Path(health_path) if health_path else None
    return {
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "head": _git("rev-parse", "HEAD"),
        "branch": _git("branch", "--show-current"),
        "working_tree_before_run": _git("status", "--short"),
        "route": "DiscoveryDebugService.discover_name(name)",
        "category_argument": "omitted",
        "input_mode": "historical archived strings; no structural corrections",
        "market": "global",
        "product_count": 50,
        "service_budget_seconds": 75,
        "service_instance": "fresh per row",
        "cache_reset_per_row": "clear_official_domain_cache (domain and surface caches)",
        "cache_sizes_before_run": {
            "official_domain": len(discovery._OFFICIAL_DOMAIN_CACHE),
            "official_surface": len(discovery._OFFICIAL_SURFACE_CACHE),
        },
        "provider_health": {
            "enabled": health.enabled,
            "persisted_file_present": bool(health_file and health_file.is_file()),
            "persisted_file_sha256": _sha256(health_file) if health_file and health_file.is_file() else None,
        },
        "provider_names": [provider.name for provider in session.providers],
        "provider_initial_local_circuits": dict(session._open_providers),
        "runtime_config": asdict(config),
        "provider_environment": {
            key: os.environ.get(key)
            for key in ("PDV_DISABLED_DISCOVERY_PROVIDERS", "PDV_BROWSER_HEADLESS")
            if key in os.environ
        },
        "pause_between_rows_seconds": 2,
        "archive_sha256": _sha256(historical_path),
        "harness_sha256": _sha256(Path("diagnostics/stage36_5_baseline.py")),
        "runner_sha256": _sha256(Path(__file__)),
        "inputs": [
            {"index": index, "name": row["input"], "identity_level": item[3],
             "category_annotation_not_passed": item[2]}
            for index, (row, item) in enumerate(zip(historical, PRODUCTS, strict=True), 1)
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    output = args.output
    if output.exists():
        parser.error(f"refusing to reuse existing output: {output}")
    manifest = preflight()
    output.mkdir(parents=True)
    (output / "preflight.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    print(f"Preflight saved: {output / 'preflight.json'}", flush=True)
    live(output / "raw")


if __name__ == "__main__":
    main()
