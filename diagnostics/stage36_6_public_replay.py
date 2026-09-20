"""Exercise saved product-page fixtures through the public name-only service.

Search results and short primary-object HTML are fixtures, not historical
network responses.  The service itself receives only the archived input line
unless ``--explicit-category`` is selected as a counterfactual.
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from unittest.mock import patch

from core.discovery import clear_official_domain_cache
from diagnostics.stage36_5_baseline import PRODUCTS, load_archive
from diagnostics.stage36_6_saved_replay import PRIMARY
from diagnostics.stage36_6_transition_audit import CONFIRMED, _key
from services.discovery_debug import DiscoveryDebugService


class SavedProvider:
    name = "saved_candidate"

    def __init__(self, url: str, title: str) -> None:
        self.url = url
        self.title = title

    def search(self, query: str):
        return [(self.url, self.title)]


ACCEPTED_PRIMARY = {
    2: "<title>SN23EI03ME Freestanding dishwasher | Siemens</title><h1>Siemens SN23EI03ME</h1>",
    7: "<title>Haier HCR5919EHMB Refrigerator</title><h1>Haier HCR5919EHMB</h1>",
    12: "<title>Roborock S8 MaxV Ultra with 8-in-1 RockDock Ultra</title><h1>Roborock S8 MaxV Ultra with 8-in-1 RockDock Ultra</h1>",
    14: "<h1>MultiQuick 9 Hand blender MQ 9187XLI</h1>",
    19: ('<h1>8-Port Gigabit Ethernet PoE+ Easy Smart Essentials Switch (62W)</h1>'
         '<script type="application/ld+json">{"@type":"Product",'
         '"name":"8-Port Gigabit Ethernet PoE+ Easy Smart Essentials Switch (62W)",'
         '"sku":"GS308EP-100NAS","model":"GS308EP"}</script>'),
    23: "<h1>Razer DeathAdder V3 Gaming Mouse</h1>",
    33: "<h1>RM850x Fully Modular Power Supply</h1>",
    43: "<h1>Frostbite Drench 39ML Fishing Lure</h1>",
    44: "<h1>Nautilus X Series XL MAX Fly Reel</h1>",
    50: "<h1>CeraVe Hydrating Facial Cleanser</h1>",
}

NEGATIVES = (
    (17, "https://www.tp-link.cz/cs/296056-tp-link-deco-be85-2ks", "TP-Link Deco BE85 2-pack"),
    (12, "https://us.roborock.com/products/s8-maxv-ultra-dust-bag", "Dust Bag for Roborock S8 MaxV Ultra"),
    (23, "https://www.razer.com/gaming-mice/razer-deathadder-v3-hyperspeed", "Razer DeathAdder V3 HyperSpeed"),
    (33, "https://www.corsair.com/us/en/p/psu/rm850x-shift", "Corsair RM850x SHIFT Power Supply"),
    (50, "https://www.cerave.com/skincare/cleansers/hydrating-facial-cleanser-refill", "Hydrating Facial Cleanser Refill"),
    (49, "https://oralb.com/en-us/products/io-series-10-twin-pack", "Oral-B iO Series 10 Twin Pack"),
    (19, "https://community.netgear.com/t5/Smart-Plus-and-Smart-Pro/GS308EP/td-p/123", "NETGEAR GS308EP forum"),
)


def replay(*, explicit_category: bool = False) -> list[dict]:
    rows = load_archive(Path("diagnostics/baselines/stage36_6/raw_sanitized.zip"))
    output = []
    for index, (page_url, _) in CONFIRMED.items():
        row = rows[index - 1]
        match = next((item for item in row["secondary"] if _key(item["url"]) == _key(page_url)), None)
        if match is None:
            output.append({"index": index, "status": "candidate_absent"})
            continue
        clear_official_domain_cache()
        provider = SavedProvider(match["url"], match["title"])
        service = DiscoveryDebugService(providers=lambda: [provider], document_reader=lambda _url: None)
        with patch("services.discovery_debug.fetch_working_page", return_value=(page_url, PRIMARY[index])):
            result = (service.discover_name(row["input"], product_category=row["category"])
                      if explicit_category else service.discover_name(row["input"]))
        output.append({
            "index": index, "status": result.status,
            "exact_official_found": result.exact_official_found,
            "queries": len(result.attempted_queries),
            "official_exact_urls": [item.url for item in result.official_pages if item.model_match == "exact"],
            "page_fetches": len(result.page_fetches),
        })
    return output


def replay_accepted(*, explicit_category: bool = False) -> list[dict]:
    path = Path("diagnostics/baselines/stage36_6/manual_exact_audit.csv")
    with path.open(newline="", encoding="utf-8") as handle:
        accepted = list(csv.DictReader(handle))
    old_rows = load_archive(Path("diagnostics/baselines/stage36_5/raw.zip"))
    output = []
    for item in accepted:
        index = int(item["index"])
        url = item["product_url"]
        brand, model, category, _ = PRODUCTS[index - 1]
        clear_official_domain_cache()
        service = DiscoveryDebugService(providers=lambda: [SavedProvider(url, f"{brand} {model}")],
                                        document_reader=lambda _url: None)
        with patch("services.discovery_debug.fetch_working_page", return_value=(url, ACCEPTED_PRIMARY[index])):
            result = (service.discover_name(old_rows[index - 1]["input"], product_category=category)
                      if explicit_category else service.discover_name(old_rows[index - 1]["input"]))
        output.append({
            "index": index, "status": result.status,
            "exact_official_found": result.exact_official_found,
            "queries": len(result.attempted_queries),
            "official_exact_urls": [page.url for page in result.official_pages if page.model_match == "exact"],
            "page_fetches": len(result.page_fetches),
        })
    return output


def replay_negatives(*, explicit_category: bool = False) -> list[dict]:
    rows = load_archive(Path("diagnostics/baselines/stage36_5/raw.zip"))
    output = []
    for index, url, heading in NEGATIVES:
        clear_official_domain_cache()
        service = DiscoveryDebugService(providers=lambda: [SavedProvider(url, heading)],
                                        document_reader=lambda _url: None)
        with patch("services.discovery_debug.fetch_working_page", return_value=(url, f"<h1>{heading}</h1>")):
            result = (service.discover_name(rows[index - 1]["input"], product_category=PRODUCTS[index - 1][2])
                      if explicit_category else service.discover_name(rows[index - 1]["input"]))
        output.append({"index": index, "url": url, "status": result.status,
                       "exact_official_found": result.exact_official_found,
                       "page_fetches": len(result.page_fetches)})
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--explicit-category", action="store_true")
    parser.add_argument("--accepted", action="store_true")
    parser.add_argument("--negatives", action="store_true")
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.all:
        result = {
            "conditions": "Saved candidate provider, short manually transcribed product HTML; public discover_name call",
            "name_only": {"confirmed_losses": replay(), "accepted_urls": replay_accepted(),
                          "negative_urls": replay_negatives()},
            "explicit_category": {
                "confirmed_losses": replay(explicit_category=True),
                "accepted_urls": replay_accepted(explicit_category=True),
                "negative_urls": replay_negatives(explicit_category=True),
            },
        }
    else:
        checked = replay_negatives if args.negatives else replay_accepted if args.accepted else replay
        result = checked(explicit_category=args.explicit_category)
    payload = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        if args.output.exists():
            raise SystemExit(f"Output already exists: {args.output}")
        args.output.write_text(payload, encoding="utf-8")
    else:
        print(payload)
