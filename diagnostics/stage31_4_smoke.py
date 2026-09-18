"""Stage 31.4 Pixel RU smoke: verify through the real service + cache, print
the Telegram-formatted RU message, official-gate record, images, and sources."""

from __future__ import annotations

import argparse
import json
import os

from bot.formatters import format_result, ordered_sources, product_image_urls
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
    report = {
        "success": result.success,
        "served_from_cache": result.served_from_cache,
        "official_source_resolution": metadata.get("official_source_resolution"),
        "ordered_sources": ordered_sources(result) if result.success else [],
        "product_images": product_image_urls(result) if result.success else [],
        "auxiliary_links": metadata.get("auxiliary_links"),
        "message": format_result(result, language=args.language),
    }
    text = json.dumps(report, ensure_ascii=False, indent=1)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(text)
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
