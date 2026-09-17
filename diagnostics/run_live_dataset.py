"""Run the live diagnostic workflow against a versioned JSON dataset."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from diagnostics.live_quality_diagnosis import PRODUCTS, build_parser, run


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, parents=[build_parser()], add_help=False)
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--ids", help="comma-separated product ids; default is the full dataset")
    args = parser.parse_args(argv)
    dataset = json.loads(Path(args.dataset).read_text(encoding="utf-8"))
    products = dataset.get("products")
    if not isinstance(products, list) or not products:
        raise ValueError("dataset must contain a non-empty products list")
    if args.ids:
        requested = {item.strip() for item in args.ids.split(",") if item.strip()}
        products = [item for item in products if item.get("id") in requested]
        found = {item.get("id") for item in products}
        missing = requested - found
        if missing:
            raise ValueError(f"unknown dataset product ids: {', '.join(sorted(missing))}")
    PRODUCTS.clear()
    for index, product in enumerate(products):
        PRODUCTS[f"dataset-{index:03d}"] = dict(product)
    args.product = "all"
    artifact = run(args)
    print(json.dumps({
        "dataset_id": dataset.get("dataset_id"),
        "records": len(artifact["records"]),
        "completed": sum(not item["timed_out"] for item in artifact["records"]),
        "service_success": sum(item["service_success"] for item in artifact["records"]),
        "output": args.output,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
