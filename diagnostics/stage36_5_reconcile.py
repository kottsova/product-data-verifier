"""Inspect saved Stage 36.5 candidates without treating automatic PASS as truth."""

from __future__ import annotations

import argparse
from pathlib import Path

from diagnostics.stage36_5_baseline import load_archive


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--archive", type=Path, default=Path("diagnostics/baselines/stage36_5/raw.zip"))
    parser.add_argument("--only", nargs="*")
    args = parser.parse_args()
    for row in load_archive(args.archive):
        if args.only and not any(term.casefold() in row["input"].casefold() for term in args.only):
            continue
        print(f"\n{row['idx']:02d} {row['input']} | {row['status']} | {row['runtime_seconds']}s")
        for key in ("official_pages", "support_pages", "documents"):
            for item in row.get(key, []):
                print(key, item.get("model_match"), item.get("url"), "|", item.get("title"))


if __name__ == "__main__":
    main()
