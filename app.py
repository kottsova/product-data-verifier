"""Small command-line entry point for the Stage 8 application workflow."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Sequence

from core.discovery import SUPPORTED_MARKETS
from core.export import export_profile_csv, export_profile_json, profile_rows, profile_to_dict
from core.quality import assess_product_quality
from core.workflow import ProductWorkflowRequest, ProductWorkflowResult, run_product_workflow


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Verify product data from collected sources.")
    parser.add_argument("brand")
    parser.add_argument("model")
    parser.add_argument("article", nargs="?")
    parser.add_argument(
        "--market",
        default="global",
        choices=sorted(SUPPORTED_MARKETS),
    )
    parser.add_argument("--max-sources", type=int, default=5)
    parser.add_argument("--no-targeted-search", action="store_true")
    parser.add_argument(
        "--format",
        choices=("table", "json", "csv"),
        default="table",
        dest="output_format",
    )
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON output.")
    parser.add_argument(
        "--include-quality",
        action="store_true",
        help="Add a top-level 'quality' key with the Stage 9 quality assessment to JSON output.",
    )
    return parser


def _single_line(value: str) -> str:
    return " ".join(value.split())


def _print_table(result: ProductWorkflowResult) -> None:
    profile = result.final_profile
    print(f"Product: {profile.identity.brand} {profile.identity.commercial_model or ''}".rstrip())
    print(
        f"Category: {profile.category.category_id} ({profile.category.confidence}) | "
        f"Discovery: {result.discovery.search_status}"
    )
    quality = assess_product_quality(profile)
    print(
        f"Quality: {quality.status} | coverage: {quality.coverage_percent}% | "
        f"critical: {quality.critical_confirmed}/{quality.critical_total} confirmed"
    )
    print("Attribute | Value | Status | Source | Evidence")
    for row in profile_rows(profile):
        print(" | ".join((
            row["Attribute"],
            _single_line(row["Value"]),
            row["Status"],
            row["Source"],
            _single_line(row["Evidence"]),
        )))


def main(argv: Sequence[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments:
        print("Product Data Verifier")
        return 0

    options = _parser().parse_args(arguments)
    try:
        request = ProductWorkflowRequest.from_parts(
            options.brand,
            options.model,
            options.article,
            market=options.market,
            max_initial_sources=options.max_sources,
            targeted_search_enabled=not options.no_targeted_search,
        )
        result = run_product_workflow(request)
    except (RuntimeError, ValueError) as error:
        print(f"Workflow failed: {error}", file=sys.stderr)
        return 1

    if options.output_format == "json":
        if options.include_quality:
            data = profile_to_dict(result.final_profile)
            data["quality"] = assess_product_quality(result.final_profile).to_dict()
            indent = 2 if options.pretty else None
            separators = None if options.pretty else (",", ":")
            print(json.dumps(data, ensure_ascii=False, indent=indent, separators=separators))
        else:
            print(export_profile_json(result.final_profile, pretty=options.pretty))
    elif options.output_format == "csv":
        sys.stdout.write(export_profile_csv(result.final_profile))
    else:
        _print_table(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
