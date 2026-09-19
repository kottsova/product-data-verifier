"""Stage 34.1 regression: document identity verdicts for benchmark products (live discovery).

Usage: python -m diagnostics.stage34_1_document_identity [--only NAME]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from core.discovery import clear_official_domain_cache
from diagnostics.stage34_extraction import slug
from services.discovery_debug import DiscoveryDebugService

PRODUCTS = (
    "Bosch HBG7741B1",
    "Philips Sonicare 9900 Prestige HX9992/12",
    "Gressel GAF-1825",
    "Makita DHP484Z",
    "DEWALT DCD796P2",
    "DeLonghi EC685M",
    "Dreame G12 Pro HHR32A",
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=Path("diagnostics/results/stage34_1"))
    parser.add_argument("--only")
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    for product in ((args.only,) if args.only else PRODUCTS):
        clear_official_domain_cache()
        result = DiscoveryDebugService().discover_name(product)
        payload = result.to_dict()
        (args.output_dir / f"{slug(product)}-discovery.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=1, default=str), encoding="utf-8",
        )
        print(f"## {product}: {result.status} pages={[p.model_match for p in result.official_pages]}", flush=True)
        for doc in result.documents:
            print(f"   {doc.doc_type:12} {doc.model_match:10} {doc.identity_evidence or '-':18} {doc.url[-70:]}", flush=True)


if __name__ == "__main__":
    main()
