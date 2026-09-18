"""Stage 32 cold Pixel smoke: EN + RU from one verification, parity check,
official-page sections/atomic facts, and the official-vs-final completeness diff.

The verification itself has no language input (core/services never see one),
so EN and RU are two *presentations* of the same result; the parity checks
below prove that presentation neither changes nor mutates the data.
"""

from __future__ import annotations

import argparse
import csv
from io import StringIO
import json

from bot.export import export_result_csv
from bot.formatters import (
    format_auxiliary,
    format_result,
    found_counts,
    ordered_sources,
    product_image_records,
)
from bot.service_factory import build_product_verifier_service
from core.official_spec_table import completeness_diff
from core.schema import get_attribute_schema
from services.product_verifier import VerifyProductRequest


def _snapshot(result) -> list[tuple]:
    return [
        (a.canonical_name, repr(a.value), a.unit, a.status, a.confidence, a.source, a.discovered)
        for a in result.attributes
    ]


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--brand", default="Google")
    p.add_argument("--model", default="Pixel 9 Pro")
    p.add_argument("--output")
    args = p.parse_args()
    from config import AppConfig

    service = build_product_verifier_service(AppConfig.from_env())
    result = service.verify(VerifyProductRequest(brand=args.brand, model=args.model))
    metadata = dict(result.metadata)
    before = _snapshot(result)

    out: dict[str, object] = {"cached": result.served_from_cache}
    for language in ("en", "ru"):
        rows = list(csv.reader(StringIO(export_result_csv(result, language=language))))
        card = format_auxiliary(result, language=language)
        out[language] = {
            "found": "%d/%d" % found_counts(result),
            "message": format_result(result, language=language),
            "sources": [url for url, _ in ordered_sources(result)],
            "card": card[0] if card else None,
            "csv_header": rows[0],
            "csv_row": rows[1],
        }
    after = _snapshot(result)

    static = {d.canonical_name for d in get_attribute_schema("smartphone")}
    official_sources = {
        url for url, is_official in ordered_sources(result) if is_official
    }
    attributes = []
    for a in result.attributes:
        if a.discovered or a.canonical_name in {"brand", "model", "manufacturer_article"}:
            continue
        origin = sorted({
            ("official" if e.source_type in {"manufacturer", "official_document"} else "secondary")
            for e in a.supporting_sources
        })
        attributes.append({
            "canonical": a.canonical_name, "status": a.status, "value": a.value, "unit": a.unit,
            "schema": "static" if a.canonical_name in static else "dynamic",
            "origin": origin, "source": a.source,
        })
    out["attributes"] = attributes

    tables = metadata.get("official_spec_table") or []
    facts = []
    ledger = []
    sections = []
    for table in tables:
        if table.get("outcome") == "extracted":
            facts.extend(table["facts"])
            ledger.extend(table["ledger"])
            sections = table["sections"]
            out["spec_page"] = table["url"]
            out["selected_column"] = table["selected_column"]
    out["sections"] = [s["heading"] for s in sections]
    out["atomic_fact_count"] = len(facts)

    by_name = {a.canonical_name: a for a in result.attributes}

    class _View:
        def __init__(self, a):
            self.status = a.status
            self.resolution_reason = "; ".join(
                f"conflicting value from {c.source}" for c in a.conflicting_values
            ) or "no supporting evidence"
            self.supporting_sources = a.supporting_sources

    diff = completeness_diff(facts, {k: _View(v) for k, v in by_name.items()})
    out["completeness"] = diff
    out["ledger"] = ledger

    parity = {
        "canonical fields identical": [a["canonical"] for a in attributes]
        == [a.canonical_name for a in result.attributes if not a.discovered
            and a.canonical_name not in {"brand", "model", "manufacturer_article"}],
        "values identical before localization": before == after,
        "sources identical": out["en"]["sources"] == out["ru"]["sources"],
        "counts identical": out["en"]["found"] == out["ru"]["found"]
        and len(out["en"]["csv_header"]) == len(out["ru"]["csv_header"])
        and len(out["en"]["csv_row"]) == len(out["ru"]["csv_row"])
        and sum(m.count(" = ") for m in out["en"]["message"]) == sum(m.count(" = ") for m in out["ru"]["message"]),
    }
    out["parity"] = {key: "PASS" if ok else "FAIL" for key, ok in parity.items()}
    out["photos"] = [
        {"role": "official" if o else "secondary", "url": u} for u, o, _ in product_image_records(result)
    ]
    text = json.dumps(out, ensure_ascii=False, indent=1, default=str)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(text)
    print(json.dumps(out["parity"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
