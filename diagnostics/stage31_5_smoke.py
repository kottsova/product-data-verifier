"""Stage 31.5 Pixel RU smoke: real service + cache, RU preview, secondary card,
photo records, /export CSV, and the official spec-table trace (which tables,
columns and sections were read, and which canonical fields came out)."""

from __future__ import annotations

import argparse
import json

from bot.export import export_result_csv
from bot.formatters import (
    format_auxiliary,
    format_result,
    found_counts,
    product_image_records,
)
from bot.service_factory import build_product_verifier_service
from services.product_verifier import VerifyProductRequest


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--brand", default="Google")
    p.add_argument("--model", default="Pixel 9 Pro")
    p.add_argument("--language", default="ru")
    p.add_argument("--output")
    args = p.parse_args()
    from config import AppConfig

    service = build_product_verifier_service(AppConfig.from_env())
    result = service.verify(VerifyProductRequest(brand=args.brand, model=args.model))
    metadata = dict(result.metadata)
    found, total = found_counts(result) if result.success else (0, 0)
    card = format_auxiliary(result, language=args.language) if result.success else None
    records = product_image_records(result) if result.success else []
    report = {
        "success": result.success,
        "served_from_cache": result.served_from_cache,
        "found": f"{found}/{total}",
        "official_source_resolution": metadata.get("official_source_resolution"),
        "official_spec_table": metadata.get("official_spec_table"),
        "source_priority": metadata.get("source_priority"),
        "message": format_result(result, language=args.language),
        "secondary_card": {"text": card[0], "preview_url": card[1]} if card else None,
        "photo_button": bool(records),
        "photos": [
            {"role": "official" if official else "secondary", "url": url, "source": source}
            for url, official, source in records
        ],
        "export_csv": export_result_csv(result, language=args.language) if result.success else "",
    }
    text = json.dumps(report, ensure_ascii=False, indent=1)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(text)
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
