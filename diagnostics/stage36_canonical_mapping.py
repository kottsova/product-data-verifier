"""Stage 36 benchmark: canonical mapping of the saved Stage 35 normalised results (offline).

Usage:
    python -m diagnostics.stage36_canonical_mapping
    python -m diagnostics.stage36_canonical_mapping --only "Makita DHP484Z" --examples 40
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from core.canonical_mapping import AMBIGUOUS, NOT_A_SPEC, UNMAPPED, CanonicalMappingResult
from core.normalization import Evidence, IdentityNode, NormalizationResult, NormalizedAttribute
from diagnostics.stage34_extraction import PRODUCTS, slug
from diagnostics.stage35_normalization import network_forbidden
from services.canonical_mapping import CanonicalMappingService


def result_from_payload(payload: dict) -> NormalizationResult:
    """Rebuild the ``NormalizationResult`` that ``NormalizationService`` produced (saved as JSON in Stage 35)."""
    names = NormalizedAttribute.__dataclass_fields__
    evidence_names = Evidence.__dataclass_fields__
    attributes = []
    for item in payload["attributes"]:
        evidence = tuple(
            Evidence(**{**{k: v for k, v in e.items() if k in evidence_names},
                        "seen_in": tuple(e.get("seen_in") or ()), "fixes": tuple(e.get("fixes") or ())})
            for e in item["evidence"]
        )
        data = {k: v for k, v in item.items() if k in names and k != "evidence"}
        for key in ("group_path", "alternates", "label_aliases", "fixes"):
            data[key] = tuple(data.get(key) or ())
        attributes.append(NormalizedAttribute(**data, evidence=evidence))
    nodes = tuple(
        IdentityNode(n["source_url"], n["role"], n["variant"], n["fields"], tuple(n["raw_indices"]))
        for n in payload.get("identity_nodes", [])
    )
    return NormalizationResult(
        product_name=payload["product_name"], brand=payload["brand"], model=payload["model"], attributes=tuple(attributes),
        identity_nodes=nodes, raw_count=payload["raw_count"], raw_by_class=payload["raw_by_class"], fixes=payload["fixes"],
    )


def markdown_table(rows: list[dict[str, object]]) -> str:
    head = "| Product | Category | Normalized specs | Mapped | Ambiguous | Unmapped | Not a spec | Unique canonical fields | Coverage |"
    lines = [head, "|" + " --- |" * 9]
    for r in rows:
        lines.append(
            f"| {r['product']} | {r['category']} ({r['category_confidence']}) | {r['normalized_specs']} | {r['mapped']} | {r['ambiguous']} | "
            f"{r['unmapped']} | {r['not_a_spec']} | {r['unique_canonical_fields']} | {r['coverage']:.0%} ({r['mapped']}/{r['eligible']}) |"
        )
    return "\n".join(lines)


def examples(results: list[CanonicalMappingResult], limit: int = 40) -> list[dict[str, str]]:
    """Round-robin over products, one row per canonical key first, so different concepts/languages show up."""
    per_product: list[list[dict[str, str]]] = []
    for result in results:
        picked, seen = [], set()
        for f in result.mapped_fields():
            if f.canonical_key in seen:
                continue
            seen.add(f.canonical_key)
            a = f.attribute
            picked.append({
                "product": result.product_name, "category": f.category,
                "normalized": f"{a.qualified_label} = {a.display or a.value}"[:80],
                "canonical": f"{f.canonical_key} = {f.normalized_value}{(' ' + f.unit) if f.unit else ''}",
                "method": f.mapping_method, "confidence": f"{f.confidence:.2f}", "unit_status": f.unit_status or "-",
            })
        per_product.append(picked)
    out, index = [], 0
    while len(out) < limit and any(index < len(p) for p in per_product):
        for picked in per_product:
            if index < len(picked) and len(out) < limit:
                out.append(picked[index])
        index += 1
    return out


def top_lists(results: list[CanonicalMappingResult], status: str, limit: int = 25) -> list[dict[str, str]]:
    rows = []
    for result in results:
        for f in result.fields:
            if f.status != status:
                continue
            a = f.attribute
            rows.append({
                "product": result.product_name, "label": a.qualified_label[:50], "value": (a.display or a.value)[:40],
                "group": " > ".join(a.group_path)[:30],
                "detail": ("; ".join(f"{c.canonical_key}@{c.confidence}" for c in f.candidates) or "; ".join(f.reasons))[:110],
            })
    return rows[:limit]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-dir", type=Path, default=Path("diagnostics/results/stage35"))
    parser.add_argument("--output-dir", type=Path, default=Path("diagnostics/results/stage36"))
    parser.add_argument("--only")
    parser.add_argument("--examples", type=int, default=40)
    parser.add_argument("--top", type=int, default=40)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    results: list[CanonicalMappingResult] = []
    for product in ((args.only,) if args.only else PRODUCTS):
        payload = json.loads((args.input_dir / f"{slug(product)}-normalized.json").read_text(encoding="utf-8"))
        with network_forbidden():
            result = CanonicalMappingService().map(result_from_payload(payload))
        results.append(result)
        (args.output_dir / f"{slug(product)}-canonical.json").write_text(
            json.dumps(result.to_dict(), ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    rows = [r.summary() for r in results]
    print(markdown_table(rows))
    print("\nCollisions:")
    for r in results:
        for c in r.collisions:
            vals = " | ".join(f"{m['normalized_value']} {m['unit']}".strip() for m in c.members)
            print(f"  {r.product_name[:14]}: {c.canonical_key} [{c.kind}] {vals}"[:200])
    exs = examples(results, args.examples)
    print(f"\nExamples ({len(exs)}):")
    for n, e in enumerate(exs, 1):
        print(f"{n:>2}. {e['product'][:14]:<14} [{e['category']}] {e['normalized']}  ->  {e['canonical']}   "
              f"({e['method']}, {e['confidence']}, unit:{e['unit_status']})")
    for status in (AMBIGUOUS, UNMAPPED, NOT_A_SPEC):
        print(f"\n{status}:")
        for row in top_lists(results, status, args.top):
            print(f"  {row['product'][:12]:<12} {row['label']!r} = {row['value']!r} [{row['group']}] -> {row['detail']}")
    (args.output_dir / "summary.json").write_text(
        json.dumps([{**r, "collisions": dict(r["collisions"])} for r in rows], ensure_ascii=False, indent=1), encoding="utf-8")
    (args.output_dir / "examples.json").write_text(
        json.dumps(examples(results, 60), ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
