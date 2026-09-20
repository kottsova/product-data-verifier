"""Make an evidence-limited row audit of all Stage 36.5 PASS losses.

The archives contain search/candidate decisions, not fetched response bodies.
In particular, a provisional page may have been fetched without a recorded
fetch result.  Never turn that missing observation into a block or mismatch.
"""

from __future__ import annotations

from collections import Counter
import csv
import json
from pathlib import Path
from urllib.parse import urlsplit

from diagnostics.stage36_5_baseline import load_archive


OLD = Path("diagnostics/baselines/stage36_5/raw.zip")
NEW = Path("diagnostics/baselines/stage36_6/raw_sanitized.zip")
OUTPUT = Path("diagnostics/baselines/stage36_6/transition_causes.csv")

# Current primary pages and audited operator relationships, reviewed
# separately from the historical archives.  These prove a present false
# refusal, not the network response the old benchmark actually received.
CONFIRMED = {
    1: ("https://www.bosch-home.co.uk/en/product/laundry/washing-machines/front-load-washing-machine/WAN28254GB", "https://media3.bsh-group.com/Documents/9001351957_A.pdf"),
    4: ("https://www.electrolux.bg/kitchen/cooking/ovens/oven/eod6p77wx/", "https://www.electrolux.bg/overlays/terms-and-conditions/"),
    5: ("https://www.aeg.fr/kitchen/cooking/hobs/induction-hob/ike64441fb/", "https://www.aeg.fr/overlays/shop-terms-and-conditions/"),
    6: ("https://www.miele.co.uk/product/11871790/t1-heat-pump-dryer-twd260wp-8kg-lotus-white", "https://www.miele.com/de/com/2185.htm"),
    16: ("https://www.asus.com/us/networking-iot-servers/wifi-routers/asus-gaming-routers/rt-be88u/", "https://www.asus.com/terms_of_use_notice_privacy_policy/official-site/"),
    22: ("https://www.logitech.com/en-us/shop/p/mx-keys-s", "https://www.logitech.com/en-us/legal/services-privacy-statement"),
    30: ("https://www.usa.canon.com/shop/p/canoscan-lide-400", "https://global.canon/ja/news/2017/20170427-2.html"),
    29: ("https://www.epson.eu/en_EU/products/printers/inkjet/consumer/ecotank-l6270-multifunction-wi-fi-ink-tank-a4-printer%2C-with-up-to-3-years-of-ink-included/p/30259", "https://www.epson.eu/en_EU/terms-of-use"),
    37: ("https://www.makita.co.nz/products/model/GA023GZ", "https://www.makita.co.nz/about/"),
    46: ("https://www.adidas.ae/en/ultraboost-5-shoes/JH9073.html", "https://www.adidas.ae/en/terms.html"),
    47: ("https://www.nike.com/dk/en/t/aeroswift-mens-dri-fit-adv-running-vest-vSX0Gdly/FN4231-010", "https://www.nike.com/be/help/a/bedrijfsgegevens/nike-contact-lijst"),
}


def _key(url: str) -> tuple[str, str]:
    parts = urlsplit(url)
    return (parts.hostname or "").lower().removeprefix("www."), parts.path.rstrip("/").casefold()


def audit(old: list[dict], new: list[dict]) -> list[dict]:
    rows = []
    for index, (before, after) in enumerate(zip(old, new, strict=True), 1):
        if before.get("status") != "PASS" or after.get("status") == "PASS":
            continue
        old_exact = [page for page in before.get("official_pages", ()) if page.get("model_match") == "exact"]
        old_keys = {_key(page["url"]) for page in old_exact}
        groups = ("official_pages", "support_pages", "documents", "dealers", "secondary", "rejected")
        matched = [(group, item) for group in groups for item in after.get(group, ()) if _key(item.get("url", "")) in old_keys]
        trace = after.get("trace", {}).get("raw_and_normalized", ())
        raw_seen = {_key(item.get("canonical_url", "")) for item in trace}
        inspections = {_key(item.get("url", "")): item for item in after.get("page_inspections", ())}
        loaded = [(group, item) for group, item in matched if _key(item["url"]) in inspections]
        official_selected = {_key(item["url"]) for item in after.get("official_pages", ())[:6]}
        provisional_selected = [
            _key(item["url"]) for item in after.get("secondary", ())
            if item.get("authority_status") == "provisional"
            and item.get("page_role") in {"product", "support"}
        ][:4]
        selected = [
            f"{item['url']}:{'official_top6' if _key(item['url']) in official_selected else 'provisional_top4' if _key(item['url']) in provisional_selected else 'not_selected'}"
            for _group, item in matched
        ]
        fetched_identity = "; ".join(dict.fromkeys(
            f"{item.get('model_match', 'unknown')}: {item.get('identity_evidence', '')}"
            for _group, item in loaded
        ))
        statuses = Counter(item.get("status") for item in after.get("provider_failures", ()))
        if index in CONFIRMED:
            verdict = "confirmed_current_false_refusal"
            page_evidence, operator_evidence = CONFIRMED[index]
        elif index == 49:
            verdict = "confirmed_old_exact_invalid_twin_pack"
            page_evidence = operator_evidence = ""
        elif index == 17:
            verdict = "confirmed_old_exact_invalid_distributor"
            page_evidence = operator_evidence = ""
        else:
            verdict = "unknown"
            page_evidence = operator_evidence = ""
        rows.append({
            "index": index, "input": after.get("input", ""),
            "identity_level": after.get("identity_level", ""),
            "old_exact_urls": " | ".join(page["url"] for page in old_exact),
            "old_exact_url_in_new_raw": sum(key in raw_seen for key in old_keys),
            "old_exact_url_in_new_candidates": len(matched),
            "new_exact_candidate_urls": " | ".join(
                item["url"] for group in groups for item in after.get(group, ())
                if item.get("model_match") == "exact" and group != "rejected"
            ),
            "candidate_decisions": " | ".join(
                f"{group}:{item.get('model_match')}:{item.get('authority_status')}:{item.get('url')}"
                for group, item in matched
            ),
            "page_check_selection": " | ".join(selected),
            "page_load_observed": " | ".join(item["url"] for _, item in loaded) or "not_recorded",
            "operator_verdict": " | ".join(sorted({str(item.get("operator_relation") or item.get("authority_status") or "unknown") for _, item in matched})) or "not_recorded",
            "main_product_verdict": fetched_identity or "not_recorded",
            "js_shell_observed": any(bool(inspections[_key(item["url"])].get("js_shell")) for _, item in loaded),
            "provider_blocked": statuses["blocked"],
            "provider_timeout": statuses["timeout"],
            "budget_over_75s": float(after.get("runtime_seconds") or 0) > 75.0,
            "fetch_limit": "provisional fetch outcomes were not archived; blocking cannot be inferred",
            "current_adjudication": verdict,
            "current_primary_page_evidence": page_evidence,
            "current_operator_evidence": operator_evidence,
        })
    return rows


def main() -> None:
    rows = audit(load_archive(OLD), load_archive(NEW))
    assert len(rows) == 26, len(rows)
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    print(json.dumps(dict(Counter(row["current_adjudication"] for row in rows)), indent=2))


if __name__ == "__main__":
    main()
