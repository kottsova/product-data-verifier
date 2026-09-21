"""Replay saved candidate URLs with small, manually transcribed page objects.

The fixture HTML contains only the primary product identity observed on the
current page.  It does not purport to reconstruct a historical HTTP response.
"""

from __future__ import annotations

import json
from pathlib import Path

from core.discovery import DiscoveryOutcome, rank_candidates
from diagnostics.stage36_5_baseline import load_archive
from diagnostics.stage36_6_transition_audit import CONFIRMED, _key
from services.discovery_debug import _result_from_outcome


PRIMARY = {
    1: "<h1>Series 4 Washing machine, front loader WAN28254GB</h1>",
    4: '<h1>Мултифункционална фурна</h1><script type="application/ld+json">{"@type":"Product","name":"Мултифункционална фурна","sku":"EOD6P77WX"}</script>',
    5: '<h1>Plaque de cuisson Induction Série 6000 Bridge</h1><script type="application/ld+json">{"@type":"Product","name":"Plaque de cuisson Induction","sku":"IKE64441FB"}</script>',
    6: "<title>TWD260WP heat pump dryer | Miele</title><h1>TWD260WP 8kg Lotus white</h1>",
    16: "<title>RT-BE88U WiFi Routers | ASUS</title><h1>ASUS RT-BE88U</h1>",
    22: "<title>Buy MX Keys S Keyboard | Logitech</title><h1>MX Keys S</h1>",
    30: "<title>CanoScan LiDE 400 Scanner | Canon</title><h1>CanoScan LiDE 400</h1>",
    29: "<h1>EcoTank L6270 Multifunction Wi-Fi Ink Tank A4 Printer</h1>",
    37: '<h1>GA023GZ 40Vmax XGT Brushless Angle Grinder</h1>',
    46: '<main><h1>Ultraboost 5 Shoes</h1><p>Product Code : JH9073</p></main>',
    47: '<title>Nike AeroSwift Men\'s Dri-FIT ADV Running Vest. Nike DK</title><main><h1>Nike AeroSwift</h1><p>Style: FN4231-010</p></main>',
    40: '<title>Einhell Planer TC-PL 750</title><h1>TC-PL 750</h1>',
    41: '<title>STIHL MS 182 Petrol Chainsaw</title><h1>MS 182 Petrol Chainsaw</h1>',
}


def replay() -> list[dict]:
    rows = load_archive(Path("diagnostics/baselines/stage36_6/raw_sanitized.zip"))
    output = []
    for index, (page_url, _evidence_url) in CONFIRMED.items():
        row = rows[index - 1]
        match = next((item for item in row["secondary"] if _key(item["url"]) == _key(page_url)), None)
        if match is None:
            output.append({"index": index, "status": "candidate_absent", "url": page_url})
            continue
        candidates = rank_candidates(
            [(match["url"], match["title"])], row["brand"], row["model"],
            product_category=row["category"],
        )
        outcome = DiscoveryOutcome(candidates, "success", [], [], [])
        result = _result_from_outcome(
            row["input"], row["brand"], row["model"], "global", outcome, 0.0,
            fetch=lambda _url, url=page_url, html=PRIMARY[index]: (url, html),
            product_category=row["category"],
        )
        output.append({
            "index": index, "status": result.status, "url": page_url,
            "authority": candidates[0]["authority_status"],
            "main_product": result.official_pages[0].model_match if result.official_pages else "not_official",
        })
    return output


if __name__ == "__main__":
    print(json.dumps(replay(), ensure_ascii=False, indent=2))
