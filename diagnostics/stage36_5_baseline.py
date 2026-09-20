"""Stage 36.5 discovery benchmark and archived-result inspection.

Live: python -m diagnostics.stage36_5_baseline --live --output DIR
Corrected inputs: add --structured-inputs (reported separately)
Replay: python -m diagnostics.stage36_5_baseline --archive diagnostics/stage36_5/raw.zip

The live run uses structured brand/model inputs. It never silently resumes an
earlier run: choose a new output directory for every run.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from zipfile import ZipFile

from core.discovery import clear_official_domain_cache
from services.discovery_debug import DiscoveryDebugService


# brand, model, category, requested identity level. The last field is the
# input contract, not a claim that an exact page exists.
PRODUCTS = (
    ("Bosch", "WAN28254GB", "major appliances", "sku"),
    ("Siemens", "SN23EI03ME", "major appliances", "sku"),
    ("Samsung", "RB38C7B6AS9", "major appliances", "sku"),
    ("Electrolux", "EOD6P77WX", "major appliances", "sku"),
    ("AEG", "IKE64441FB", "major appliances", "sku"),
    ("Miele", "TWD260WP", "major appliances", "sku"),
    ("Haier", "HCR5919EHMB", "major appliances", "sku"),
    ("DeLonghi", "Eletta Explore ECAM450.86.T", "small appliances", "sku"),
    ("Philips", "Airfryer Combi XXL HD9876/90", "small appliances", "sku"),
    ("Dyson", "V15 Detect", "small appliances", "model"),
    ("Kärcher", "K5 Power Control", "small appliances", "model"),
    ("Roborock", "S8 MaxV Ultra", "small appliances", "model"),
    ("Tefal", "Ultimate Pure FV9845", "small appliances", "sku"),
    ("Braun", "MultiQuick 9 MQ9187XLI", "small appliances", "sku"),
    ("Kenwood", "Titanium Chef Patissier XL KWL90", "small appliances", "model"),
    ("ASUS", "RT-BE88U", "networking", "sku"),
    ("TP-Link", "Deco BE85", "networking", "model"),
    ("Ubiquiti", "UniFi U7 Pro", "networking", "model"),
    ("NETGEAR", "GS308EP", "networking", "sku"),
    ("Synology", "DS923+", "networking", "sku"),
    ("APC", "Back-UPS BX1600MI", "computer/peripherals", "sku"),
    ("Logitech", "MX Keys S", "computer/peripherals", "model"),
    ("Razer", "DeathAdder V3", "computer/peripherals", "model"),
    ("SteelSeries", "Arctis Nova Pro Wireless", "computer/peripherals", "model"),
    ("Samsung", "Odyssey OLED G8 G80SD", "TV/display", "model"),
    ("LG", "OLED evo C4", "TV/display", "family"),
    ("Apple", "Mac mini M4", "computer/peripherals", "family"),
    ("Lenovo", "ThinkPad X1 Carbon Gen 13 Aura Edition", "computer/peripherals", "model"),
    ("Epson", "EcoTank L6270", "computer/peripherals", "sku"),
    ("Canon", "CanoScan LiDE 400", "computer/peripherals", "model"),
    ("WD", "My Passport 5TB", "PC components", "family"),
    ("Crucial", "T705", "PC components", "family"),
    ("Corsair", "RM850x", "PC components", "family"),
    ("ASUS", "TUF Gaming GeForce RTX 5070 Ti", "PC components", "family"),
    ("MSI", "MAG X870 TOMAHAWK WIFI", "PC components", "model"),
    ("DEWALT", "DCD805P2T", "power tools", "sku"),
    ("Makita", "GA023GZ", "power tools", "sku"),
    ("Milwaukee", "M18 FHX-502X", "power tools", "sku"),
    ("Bosch", "Professional GCM 8 SJL", "power tools", "model"),
    ("Einhell", "TC-PL 750", "power tools", "model"),
    ("STIHL", "MS 182", "garden/outdoor tools", "model"),
    ("Husqvarna", "525LK", "garden/outdoor tools", "sku"),
    ("Frostbite", "Drench 39ML", "fishing", "model"),
    ("Nautilus", "X Series XL MAX", "fishing", "model"),
    ("Namazu", "Bionic 90 N41-90-52", "fishing", "sku"),
    ("adidas", "Ultraboost 5 JH9073", "apparel/footwear", "sku"),
    ("Nike", "AeroSwift FN4231-010", "apparel/footwear", "sku"),
    ("The North Face", "Nuptse", "apparel/footwear", "family"),
    ("Oral-B", "iO Series 10", "personal care/skincare", "model"),
    ("CeraVe", "Hydrating Facial Cleanser", "personal care/skincare", "family"),
)


def load_archive(path: Path) -> list[dict]:
    with ZipFile(path) as archive:
        return [json.loads(archive.read(f"{i:02d}.json")) for i in range(1, 51)]


def summary(rows: list[dict]) -> dict:
    return {
        "runs": len(rows),
        "automatic_pass": sum(row.get("status") == "PASS" for row in rows),
        "automatic_exact_product": sum(bool(row.get("exact_official_found")) for row in rows),
        "automatic_any_official": sum(bool(row.get("official") or row.get("documents")) for row in rows),
        "documents_listed": sum(len(row.get("documents", [])) for row in rows),
        "provider_failures": sum(len(row.get("provider_failures", [])) for row in rows),
        "runtime_seconds": round(sum(float(row.get("runtime_seconds") or 0) for row in rows), 1),
    }


def manifest_rows(rows: list[dict]) -> list[dict]:
    """Recorded input and automatic outcome; no manual adjudication implied."""
    return [{
        "index": row.get("idx"), "input": row.get("input"),
        "parsed_brand": row.get("brand"), "parsed_model": row.get("model"),
        "category": row.get("category"), "status": row.get("status"),
        "exact_official_found": row.get("exact_official_found"),
        "official_pages": len(row.get("official_pages", [])),
        "support_pages": len(row.get("support_pages", [])),
        "documents": len(row.get("documents", [])),
    } for row in rows]


def live(
    output: Path, *, structured_inputs: bool = False,
    max_items: int = 50, selected_indices: set[int] | None = None,
) -> None:
    if output.exists():
        raise SystemExit(f"Output directory already exists: {output}")
    output.mkdir(parents=True)
    historical = load_archive(Path("diagnostics/baselines/stage36_5/raw.zip"))
    for i, (brand, model, category, level) in enumerate(PRODUCTS, 1):
        if i > max_items:
            break
        if selected_indices is not None and i not in selected_indices:
            continue
        clear_official_domain_cache()
        name = f"{brand} | {model}" if structured_inputs else historical[i - 1]["input"]
        started = time.monotonic()
        try:
            # A fresh service per row matches the archived harness.
            result = DiscoveryDebugService().discover_name(name, product_category=category)
            row = result.to_dict()
        except Exception as exc:  # preserve each failed attempt
            row = {"error": repr(exc)}
        row.update(index=i, input=name, category=category, identity_level=level,
                   input_mode="structured" if structured_inputs else "historical")
        (output / f"{i:02d}.json").write_text(json.dumps(row, ensure_ascii=False, indent=1), encoding="utf-8")
        print(i, row["input"], row.get("status", row.get("error")), round(time.monotonic() - started, 1), flush=True)
        time.sleep(2)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--rows", action="store_true")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--structured-inputs", action="store_true")
    parser.add_argument("--max-items", type=int, default=50)
    parser.add_argument("--indices", help="Comma-separated row numbers for a separate diagnostic run")
    args = parser.parse_args()
    if args.live:
        if args.output is None:
            parser.error("--live requires --output")
        if not 1 <= args.max_items <= 50:
            parser.error("--max-items must be between 1 and 50")
        selected = None
        if args.indices:
            try:
                selected = {int(part) for part in args.indices.split(",")}
            except ValueError:
                parser.error("--indices must be comma-separated integers")
            if not selected or any(index < 1 or index > args.max_items for index in selected):
                parser.error("--indices must be within 1..max-items")
        live(args.output, structured_inputs=args.structured_inputs, max_items=args.max_items,
             selected_indices=selected)
    elif args.archive:
        rows = load_archive(args.archive)
        print(json.dumps(manifest_rows(rows) if args.rows else summary(rows), ensure_ascii=False, indent=2))
    else:
        parser.error("provide --archive or --live")


if __name__ == "__main__":
    main()
