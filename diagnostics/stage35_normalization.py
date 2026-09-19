"""Stage 35 benchmark: normalise the saved Stage 34 raw extractions (no network, no discovery).

Loads ``diagnostics/results/stage34/<product>-extraction.json`` into ``RawExtractionResult`` objects,
runs ``NormalizationService`` with every socket operation forbidden, and prints the comparison table
and raw -> normalized examples.

Usage:
    python -m diagnostics.stage35_normalization
    python -m diagnostics.stage35_normalization --input-dir diagnostics/results/stage34 --only "Makita DHP484Z"
"""

from __future__ import annotations

import argparse
from collections import Counter
from contextlib import contextmanager
import json
from pathlib import Path
import socket
from unittest import mock

from core.normalization import CLASSES, IDENTITY, SPEC, NormalizationResult
from core.raw_extraction import RawAttribute
from diagnostics.stage34_extraction import PRODUCTS, slug
from services.normalization import NormalizationService
from services.raw_extraction import RawExtractionResult, SourceExtraction


def extraction_from_payload(payload: dict) -> RawExtractionResult:
    """Rebuild the ``RawExtractionResult`` that ``RawExtractionService`` produced (saved as JSON in Stage 34)."""
    names = RawAttribute.__dataclass_fields__

    def source(item: dict) -> SourceExtraction:
        attributes = tuple(
            RawAttribute(**{**{k: v for k, v in a.items() if k in names}, "seen_in": tuple(a.get("seen_in") or ())})
            for a in item.get("attributes", [])
        )
        keep = {k: v for k, v in item.items() if k in SourceExtraction.__dataclass_fields__ and k not in {"attributes", "issues"}}
        return SourceExtraction(**keep, attributes=attributes, issues=tuple(item.get("issues") or ()))

    return RawExtractionResult(
        product_name=payload["product_name"], brand=payload["brand"], model=payload["model"],
        discovery_status=payload["discovery_status"],
        pages=tuple(source(s) for s in payload.get("pages", [])),
        support_pages=tuple(source(s) for s in payload.get("support_pages", [])),
        documents=tuple(source(s) for s in payload.get("documents", [])),
        skipped=tuple(payload.get("skipped", [])), runtime_seconds=payload.get("runtime_seconds", 0.0),
    )


@contextmanager
def network_forbidden():
    def forbidden(*_a, **_k):
        raise AssertionError("normalisation attempted a network operation")

    with mock.patch.object(socket.socket, "connect", forbidden), \
         mock.patch.object(socket.socket, "connect_ex", forbidden), \
         mock.patch("socket.getaddrinfo", forbidden), \
         mock.patch("socket.create_connection", forbidden):
        yield


def multi_valued_labels(result: NormalizationResult) -> int:
    """Spec labels that carry several distinct values *inside one source* (a later conflict-resolution input).

    The same label with translated values on sibling-locale pages is not counted: that is one attribute in two languages.
    """
    values: dict[tuple[str, str], set[tuple[str, str]]] = {}
    for a in result.of_class(SPEC):
        for url in a.source_urls:
            values.setdefault((url, a.qualified_label.casefold()), set()).add((a.value, a.unit))
    return len({label for (_, label), v in values.items() if len(v) > 1})


def summary_row(result: NormalizationResult) -> dict[str, object]:
    raw = result.raw_by_class
    specs = len(result.of_class(SPEC))
    raw_specs = raw[SPEC]
    reasons = Counter(a.class_reason for a in result.attributes if a.cls in {"unknown", "marketing_content"})
    variants = [n for n in result.identity_nodes if n.role == "variant"]
    issues = []
    if multi_valued_labels(result):
        issues.append(f"{multi_valued_labels(result)} multi-valued spec labels")
    if reasons.get("unresolved_i18n_key"):
        issues.append(f"{reasons['unresolved_i18n_key']} unresolved i18n")
    if reasons.get("placeholder_value") or reasons.get("template_text") or reasons.get("template_block"):
        issues.append(f"{reasons.get('placeholder_value', 0) + reasons.get('template_text', 0) + reasons.get('template_block', 0)} placeholder/template")
    if variants:
        issues.append(f"{len(variants)} variant nodes (kept apart)")
    if not specs:
        issues.append("no product specs")
    return {
        "product": result.product_name, "raw": result.raw_count, "product_specs": raw_specs,
        "metadata": raw[IDENTITY], "marketing_other": raw["marketing_content"] + raw["unknown"],
        "normalized_specs": specs, "normalized_total": len(result.attributes),
        "dedup": round(1 - specs / raw_specs, 3) if raw_specs else 0.0,
        "issues": issues, "fixes": result.fixes, "class_reasons": dict(reasons),
    }


def examples(results: list[NormalizationResult], limit: int = 20) -> list[dict[str, str]]:
    """Round-robin over products: prefer records where the normaliser visibly changed something."""
    per_product: list[list[dict[str, str]]] = []
    for result in results:
        picked, seen_fix = [], set()
        for a in result.attributes:
            first = a.evidence[0]
            changed = (first.raw_label, first.raw_value) != (a.label, a.display or a.value)
            fixes = tuple(f for f in a.fixes if f not in {"unicode_variant", "punctuation"})
            key = (a.cls, fixes[:1] or (a.class_reason,))
            if not changed or key in seen_fix:
                continue
            seen_fix.add(key)
            picked.append({
                "product": result.product_name, "raw": f"{first.raw_label!r} = {first.raw_value!r}"[:110],
                "normalized": f"[{a.cls}] {a.qualified_label} = {a.display or a.value}"[:110],
                "fixes": ", ".join(a.fixes) or a.class_reason, "evidence": str(len(a.evidence)),
            })
        per_product.append(picked)
    out, round_index = [], 0
    while len(out) < limit and any(round_index < len(p) for p in per_product):
        for picked in per_product:
            if round_index < len(picked) and len(out) < limit:
                out.append(picked[round_index])
        round_index += 1
    return out


def markdown_table(rows: list[dict[str, object]]) -> str:
    head = "| Product | Raw | Product specs | Metadata | Marketing/other | Normalized unique specs | Dedup ratio | Issues |"
    lines = [head, "|" + " --- |" * 8]
    for r in rows:
        lines.append(
            f"| {r['product']} | {r['raw']} | {r['product_specs']} | {r['metadata']} | {r['marketing_other']} | "
            f"{r['normalized_specs']} | {r['dedup']:.0%} | {'; '.join(r['issues']) or '-'} |"
        )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-dir", type=Path, default=Path("diagnostics/results/stage34"))
    parser.add_argument("--output-dir", type=Path, default=Path("diagnostics/results/stage35"))
    parser.add_argument("--only")
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    results: list[NormalizationResult] = []
    for product in ((args.only,) if args.only else PRODUCTS):
        payload = json.loads((args.input_dir / f"{slug(product)}-extraction.json").read_text(encoding="utf-8"))
        extraction = extraction_from_payload(payload)
        with network_forbidden():
            result = NormalizationService().normalize(extraction)
        results.append(result)
        (args.output_dir / f"{slug(product)}-normalized.json").write_text(
            json.dumps(result.to_dict(), ensure_ascii=False, indent=1, default=str), encoding="utf-8",
        )
    rows = [summary_row(r) for r in results]
    print(markdown_table(rows))
    print()
    for row in rows:
        print(f"{row['product']}: fixes={row['fixes']} reasons={row['class_reasons']}")
    print()
    for n, item in enumerate(examples(results), 1):
        print(f"{n:>2}. {item['product']} | {item['raw']}\n      -> {item['normalized']}   ({item['fixes']}; evidence {item['evidence']})")
    (args.output_dir / "summary.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    (args.output_dir / "examples.json").write_text(json.dumps(examples(results, 40), ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
