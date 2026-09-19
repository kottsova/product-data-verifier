"""Stage 34 benchmark: raw attribute extraction from DiscoveryDebugResult.

Per product: run discovery once (or load a saved discovery payload with
``--from-saved``), then hand the *result object* to ``RawExtractionService``.
While extraction runs, every discovery entry point is patched to raise, so a
passing run proves that official sources were not searched again.

Usage:
    python -m diagnostics.stage34_extraction                 # live discovery + extraction
    python -m diagnostics.stage34_extraction --from-saved    # re-extract from saved discovery JSON
    python -m diagnostics.stage34_extraction --only "DEWALT DCD796P2"
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
from pathlib import Path
import re
from unittest import mock

from core.discovery import clear_official_domain_cache
from core.official_documents import OfficialDocument
from services.discovery_debug import DiscoveryDebugResult, DiscoveryDebugService, DiscoverySource
from services.raw_extraction import ExtractionResult, RawExtractionService

PRODUCTS = (
    "Gressel GAF-1825",
    "Bosch HBG7741B1",
    "DEWALT DCD796P2",
    "Philips Sonicare 9900 Prestige HX9992/12",
    "Makita DHP484Z",
    "DeLonghi EC685M",
    "Logitech MX Master 3S",
    "The Ordinary Niacinamide 10% + Zinc 1%",
)


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.casefold()).strip("-")


def discovery_from_payload(payload: dict) -> DiscoveryDebugResult:
    """Rebuild the fields extraction reads from a saved discovery JSON."""
    def sources(items: list[dict]) -> tuple[DiscoverySource, ...]:
        names = DiscoverySource.__dataclass_fields__
        return tuple(DiscoverySource(**{k: v for k, v in item.items() if k in names}) for item in items)

    def documents(items: list[dict]) -> tuple[OfficialDocument, ...]:
        names = OfficialDocument.__dataclass_fields__
        out = []
        for item in items:
            data = {k: v for k, v in item.items() if k in names}
            data["found_on"] = tuple(data.get("found_on") or ())
            data["locales"] = tuple(data.get("locales") or ())
            out.append(OfficialDocument(**data))
        return tuple(out)

    return DiscoveryDebugResult(
        product_name=payload["product_name"], brand=payload["brand"], model=payload["model"],
        market=payload.get("market", ""), status=payload["status"],
        exact_official_found=payload["exact_official_found"],
        official=sources(payload.get("official", [])), dealers=(), secondary=(), rejected=(),
        runtime_seconds=payload.get("runtime_seconds", 0.0), search_status=payload.get("search_status", ""),
        attempted_queries=(), providers=(), provider_failures=(), candidate_counts={},
        official_pages=sources(payload.get("official_pages", [])),
        support_pages=sources(payload.get("support_pages", [])),
        documents=documents(payload.get("documents", [])),
    )


@contextmanager
def searching_forbidden():
    """Any discovery/search entry point called during extraction raises."""
    def forbidden(*_a, **_k):
        raise AssertionError("extraction attempted to search for sources")

    with mock.patch("core.discovery.discover_with_status", forbidden), \
         mock.patch("services.discovery_debug.discover_with_status", forbidden), \
         mock.patch.object(DiscoveryDebugService, "discover_name", forbidden):
        yield


def coverage_row(result: ExtractionResult) -> dict[str, object]:
    sources = result.sources
    fetched = [s for s in sources if s.fetched]
    with_attrs = [s for s in sources if s.attributes]
    locations: dict[str, int] = {}
    for a in result.attributes:
        locations[a.location] = locations.get(a.location, 0) + 1
    issues = sorted({f"{s.url.split('/')[2] if s.url.count('/') >= 2 else s.url}: {i}" for s in sources for i in s.issues})
    return {
        "product": result.product_name,
        "official_sources": len(sources),
        "pages": len(result.pages), "support": len(result.support_pages), "documents": len(result.documents),
        "fetched": len(fetched), "sources_with_attributes": len(with_attrs),
        "raw_attributes": len(result.attributes),
        "by_location": locations,
        "issues": issues,
    }


def run(product: str, output_dir: Path, from_saved: bool) -> ExtractionResult:
    disco_path = output_dir / f"{slug(product)}-discovery.json"
    if from_saved and disco_path.exists():
        discovery = discovery_from_payload(json.loads(disco_path.read_text(encoding="utf-8")))
    else:
        clear_official_domain_cache()
        discovery = DiscoveryDebugService().discover_name(product)
        disco_path.write_text(json.dumps(discovery.to_dict(), ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    with searching_forbidden():
        result = RawExtractionService().extract(discovery)
    (output_dir / f"{slug(product)}-extraction.json").write_text(
        json.dumps(result.to_dict(), ensure_ascii=False, indent=1, default=str), encoding="utf-8",
    )
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=Path("diagnostics/results/stage34"))
    parser.add_argument("--from-saved", action="store_true")
    parser.add_argument("--only")
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    for product in ((args.only,) if args.only else PRODUCTS):
        result = run(product, args.output_dir, args.from_saved)
        row = coverage_row(result)
        rows.append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)
    (args.output_dir / "coverage.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
