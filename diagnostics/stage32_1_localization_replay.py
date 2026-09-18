"""Replay Stage 32.1 presentation from one cached canonical result.

The cache is opened read-only.  This diagnostic never constructs the service,
calls a provider, performs discovery/fetch/extraction, or sends Telegram data.
"""

from __future__ import annotations

import argparse
import csv
from io import StringIO
import json
from pathlib import Path
import sqlite3

from bot.attribute_filter import filter_user_facing
from bot.export import export_result_csv
from bot.formatters import format_result, found_counts, ordered_sources
from bot.i18n import display_name
from bot.localize import localize_value
from services.product_verifier import _result_from_dict


def _load_cached_result(path: Path, brand: str, model: str):
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        payloads = connection.execute(
            "SELECT payload FROM verification_cache ORDER BY stored_at DESC"
        ).fetchall()
    for (payload_text,) in payloads:
        payload = json.loads(payload_text)
        request = payload.get("request") or {}
        if (
            str(request.get("brand", "")).casefold() == brand.casefold()
            and str(request.get("model", "")).casefold() == model.casefold()
        ):
            return _result_from_dict(payload)
    raise LookupError(f"no cached result for {brand} {model}")


def _snapshot(result) -> dict[str, object]:
    return {
        "attributes": [
            (
                item.canonical_name, repr(item.value), item.unit, item.status,
                item.source, tuple(source.source for source in item.supporting_sources),
            )
            for item in result.attributes
        ],
        "conflicts": tuple(result.conflicts),
        "unresolved": tuple(result.unresolved),
    }


def _localized_examples(result) -> dict[str, dict[str, str]]:
    wanted = (
        "display_size", "battery_capacity", "camera_features", "color",
        "package_contents", "sensors", "authentication", "fast_charging",
        "wireless_charging", "materials_and_durability", "haptic_engine",
        "processor", "usb", "wifi", "bluetooth", "ip_rating",
    )
    by_name = {item.canonical_name: item for item in result.attributes}
    examples: dict[str, dict[str, str]] = {}
    for canonical in wanted:
        item = by_name.get(canonical)
        if item is None or item.value is None:
            continue
        text = str(item.value)
        if item.unit and not text.casefold().rstrip().endswith(str(item.unit).casefold()):
            text = f"{text} {item.unit}"
        examples[canonical] = {
            "en_label": display_name(canonical, "en", fallback=item.display_name),
            "en_value": localize_value(canonical, text, item.unit, "en"),
            "ru_label": display_name(canonical, "ru", fallback=item.display_name),
            "ru_value": localize_value(canonical, text, item.unit, "ru"),
        }
    return examples


def build_report(result) -> dict[str, object]:
    before = _snapshot(result)
    presentations: dict[str, dict[str, object]] = {}
    for language in ("en", "ru"):
        csv_rows = list(csv.reader(StringIO(export_result_csv(result, language=language))))
        presentations[language] = {
            "found": "%d/%d" % found_counts(result),
            "preview_attribute_count": sum(
                line.count(" = ") for line in format_result(result, language=language)
            ),
            "csv_column_count": len(csv_rows[0]),
            "csv_populated_count": sum(
                bool(value and value not in {"Not found", "Не найдено"})
                for value in csv_rows[1]
            ),
            "sources": [url for url, _ in ordered_sources(result)],
        }
    after = _snapshot(result)
    attributes = filter_user_facing(result.attributes)
    return {
        "request": result.request.to_dict(),
        "canonical_attribute_count": len(result.attributes),
        "user_facing_canonical_order": [item.canonical_name for item in attributes],
        "conflicts": list(result.conflicts),
        "unresolved": list(result.unresolved),
        "presentations": presentations,
        "examples": _localized_examples(result),
        "parity": {
            "canonical_values_sources_conflicts_unresolved_unchanged": before == after,
            "counts_identical": presentations["en"]["found"] == presentations["ru"]["found"],
            "preview_shape_identical": presentations["en"]["preview_attribute_count"]
            == presentations["ru"]["preview_attribute_count"],
            "csv_schema_size_identical": presentations["en"]["csv_column_count"]
            == presentations["ru"]["csv_column_count"],
            "csv_population_identical": presentations["en"]["csv_populated_count"]
            == presentations["ru"]["csv_populated_count"],
            "sources_identical": presentations["en"]["sources"] == presentations["ru"]["sources"],
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache", type=Path, default=Path(".cache/product_verifier.sqlite3"))
    parser.add_argument("--brand", default="Google")
    parser.add_argument("--model", default="Pixel 9 Pro")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = _load_cached_result(args.cache, args.brand, args.model)
    text = json.dumps(build_report(result), ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
