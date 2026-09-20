"""Small, auditable bootstrap for brand-operated web hosts.

The registry is evidence, not a brand-specific decision algorithm. Each entry
identifies one host, one brand and an audited operator relationship. Search
snippets, same-looking domain names and links from unverified sites cannot
create entries. Hosts are exact: a seed does not implicitly cover arbitrary
subdomains, sibling TLDs, external file stores or other group brands.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, timedelta

from core.match import normalize_model


RULES_VERSION = 1
MAX_AGE = timedelta(days=365)


@dataclass(frozen=True, slots=True)
class AuthoritySeed:
    brand: str
    host: str
    operator_relation: str
    scope: str
    evidence_url: str
    checked_on: date
    evidence_excerpt: str = ""
    rules_version: int = RULES_VERSION

    @property
    def first_party(self) -> bool:
        return self.operator_relation in {"brand_operator", "licensed_brand_operator"}


# Reviewed brand/company pages. "licensed_brand_operator" remains a distinct
# relationship even when its product pages are accepted as first-party brand
# documentation. A dealer is never entered as a manufacturer seed.
SEEDS = (
    AuthoritySeed("Siemens", "siemens-home.bsh-group.com", "licensed_brand_operator", "home appliances", "https://press.siemens-home.bsh-group.com/", date(2026, 9, 20)),
    AuthoritySeed("Haier", "haier-europe.com", "brand_operator", "European appliances", "https://corporate.haier-europe.com/our-brands/haier/", date(2026, 9, 20)),
    AuthoritySeed("Philips", "home-appliances.philips", "licensed_brand_operator", "domestic appliances", "https://www.philips.com/c-dam/corporate/newscenter/de/press-releases/2023/202302-philips-da-wird-versuni/2302_PRESSEINFORMATION_Versuni.pdf", date(2026, 9, 20)),
    AuthoritySeed("Braun", "braunhousehold.com", "licensed_brand_operator", "household appliances", "https://www.delonghigroup.com/en/brand/braun", date(2026, 9, 20)),
    AuthoritySeed("Kenwood", "kenwoodworld.com", "brand_operator", "household appliances", "https://www.delonghigroup.com/en/brand/kenwood", date(2026, 9, 20)),
    AuthoritySeed("Bosch", "bosch-professional.com", "brand_operator", "professional power tools", "https://www.bosch-professional.com/", date(2026, 9, 20)),
    AuthoritySeed("TP-Link", "tp-link.com", "brand_operator", "networking", "https://www.tp-link.com/", date(2026, 9, 20)),
    AuthoritySeed("TP-Link", "tp-link.cz", "independent_distributor", "Czech retail", "https://www.tp-link.cz/cs/static/page/5", date(2026, 9, 20)),
    AuthoritySeed("NETGEAR", "netgear.com", "brand_operator", "networking", "https://www.netgear.com/business/wired/switches/easy-smart/gs308ep/", date(2026, 9, 20)),
    AuthoritySeed("Razer", "razer.com", "brand_operator", "gaming peripherals", "https://www.razer.com/gaming-mice/razer-deathadder-v3", date(2026, 9, 20)),
    AuthoritySeed("Corsair", "corsair.com", "brand_operator", "PC components", "https://www.corsair.com/", date(2026, 9, 20)),
    AuthoritySeed("Roborock", "us.roborock.com", "brand_operator", "robot vacuums", "https://us.roborock.com/products/roborock-s8-maxv-ultra", date(2026, 9, 20)),
    AuthoritySeed("CeraVe", "cerave.com", "brand_operator", "skin care", "https://www.cerave.com/skincare/cleansers/hydrating-facial-cleanser", date(2026, 9, 20)),
    AuthoritySeed("Oral-B", "oralb.com", "brand_operator", "oral care", "https://oralb.com/en-us/products/", date(2026, 9, 20)),
    AuthoritySeed("Frostbite", "fishfrostbite.com", "brand_operator", "fishing tackle", "https://fishfrostbite.com/products/drench-39ml", date(2026, 9, 20)),
    AuthoritySeed("Nautilus", "nautilusreels.com", "brand_operator", "fishing reels", "https://www.nautilusreels.com/products/x-series-xl-max", date(2026, 9, 20)),
)

# Short source fragments or tightly scoped paraphrases retained with each
# human-reviewed seed. These are provenance, not an automatic self-claim rule.
_EVIDENCE_EXCERPTS = {
    "siemens-home.bsh-group.com": "BSH press portal for Siemens home appliances",
    "haier-europe.com": "Haier Europe corporate brand and customer-care pages name this Haier site",
    "home-appliances.philips": "Philips states Versuni is licensee for Philips domestic appliances",
    "braunhousehold.com": "De'Longhi identifies Braun as its licensed household-appliance brand",
    "kenwoodworld.com": "De'Longhi identifies Kenwood in its household-appliance portfolio",
    "bosch-professional.com": "Bosch corporate brochure names bosch-professional.com for power tools",
    "tp-link.com": "TP-Link product site for its networking range",
    "tp-link.cz": "The store terms name 100Mega Distribution s.r.o. as the operator",
    "netgear.com": "NETGEAR product site for GS308EP",
    "razer.com": "Razer product site for DeathAdder V3",
    "corsair.com": "Corsair product site for PC components",
    "us.roborock.com": "Roborock US product site for S8 MaxV Ultra",
    "cerave.com": "CeraVe product site for skin care",
    "oralb.com": "Oral-B product site for oral care",
    "fishfrostbite.com": "Fish Frostbite company page describes its fishing brand",
    "nautilusreels.com": "Nautilus company page describes design and manufacture of its reels",
}
SEEDS = tuple(replace(seed, evidence_excerpt=_EVIDENCE_EXCERPTS[seed.host]) for seed in SEEDS)


def find_seed(brand: str, host: str, *, today: date | None = None) -> AuthoritySeed | None:
    now = today or date.today()
    brand_key = normalize_model(brand)
    host = host.lower().removeprefix("www.").rstrip(".")
    return next((
        seed for seed in SEEDS
        if normalize_model(seed.brand) == brand_key
        and seed.host == host
        and seed.rules_version == RULES_VERSION
        and timedelta(0) <= now - seed.checked_on <= MAX_AGE
    ), None)
