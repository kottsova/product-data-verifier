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
    categories: tuple[str, ...] = ()

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
    AuthoritySeed("Frostbite", "fishfrostbite.com", "operator_unknown", "fishing tackle", "https://fishfrostbite.com/pages/contact", date(2026, 9, 20)),
    AuthoritySeed("Nautilus", "nautilusreels.com", "operator_unknown", "fishing reels", "https://www.nautilusreels.com/pages/about", date(2026, 9, 20)),
    AuthoritySeed("Bosch", "bosch-home.co.uk", "licensed_brand_operator", "UK home appliances", "https://media3.bsh-group.com/Documents/9001351957_A.pdf", date(2026, 9, 20)),
    AuthoritySeed("Electrolux", "electrolux.bg", "brand_operator", "Bulgarian home appliances", "https://www.electrolux.bg/overlays/terms-and-conditions/", date(2026, 9, 20)),
    AuthoritySeed("AEG", "aeg.fr", "licensed_brand_operator", "French home appliances", "https://www.aeg.fr/overlays/shop-terms-and-conditions/", date(2026, 9, 20)),
    AuthoritySeed("Miele", "miele.co.uk", "brand_operator", "UK home appliances", "https://www.miele.com/de/com/2185.htm", date(2026, 9, 20)),
    AuthoritySeed("ASUS", "asus.com", "brand_operator", "networking and PC components", "https://www.asus.com/terms_of_use_notice_privacy_policy/official-site/", date(2026, 9, 20)),
    AuthoritySeed("Logitech", "logitech.com", "brand_operator", "computer peripherals", "https://www.logitech.com/en-us/legal/services-privacy-statement", date(2026, 9, 20)),
    AuthoritySeed("Canon", "usa.canon.com", "brand_operator", "US scanners", "https://global.canon/ja/news/2017/20170427-2.html", date(2026, 9, 20)),
    AuthoritySeed("Epson", "epson.eu", "brand_operator", "European printers and scanners", "https://www.epson.eu/en_EU/terms-of-use", date(2026, 9, 20)),
    AuthoritySeed("Makita", "makita.co.nz", "brand_operator", "New Zealand power tools", "https://www.makita.co.nz/about/", date(2026, 9, 20)),
    AuthoritySeed("adidas", "adidas.ae", "brand_operator", "UAE apparel and footwear", "https://www.adidas.ae/en/terms.html", date(2026, 9, 20)),
    AuthoritySeed("Nike", "nike.com", "brand_operator", "Nike apparel and footwear", "https://www.nike.com/be/help/a/bedrijfsgegevens/nike-contact-lijst", date(2026, 9, 20)),
    AuthoritySeed("Samsung", "samsung.com", "brand_operator", "Samsung major appliances and displays", "https://news.samsung.com/global/terms", date(2026, 9, 20)),
    AuthoritySeed("DEWALT", "dewalt.co.uk", "licensed_brand_operator", "UK power tools", "https://www.dewalt.co.uk/en-gb/terms-use", date(2026, 9, 20)),
    AuthoritySeed("Einhell", "einhell.co.uk", "brand_operator", "UK power tools", "https://www.einhell.co.uk/about-us/", date(2026, 9, 20)),
    AuthoritySeed("STIHL", "stihl.co.uk", "brand_operator", "UK garden and outdoor tools", "https://www.stihl.co.uk/en/legal-info/terms-of-use", date(2026, 9, 20)),
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
    "bosch-home.co.uk": "BSH appliance documentation identifies BSH Home Appliances Ltd and bosch-home.co.uk",
    "electrolux.bg": "Site terms identify AB Electrolux as publisher of the Bulgarian appliance site",
    "aeg.fr": "Electrolux France sales terms explicitly identify aeg.fr as its AEG site",
    "miele.co.uk": "Miele corporate locations identify Miele UK and miele.co.uk",
    "asus.com": "ASUSTeK legal terms identify ASUS as operator of this product site",
    "logitech.com": "Logitech privacy statement identifies Logitech International and its web sites",
    "usa.canon.com": "Canon global names Canon U.S.A. and its usa.canon.com site",
    "epson.eu": "Epson Europe B.V. site terms identify the regional site operator",
    "makita.co.nz": "Makita NZ names itself a subsidiary; corporate history confirms the subsidiary",
    "adidas.ae": "Site terms identify adidas AG as retailer and brand-content owner; Global-e facilitates checkout",
    "nike.com": "Nike company details identify Nike Retail B.V. as nike.com operator",
    "samsung.com": "Samsung Electronics newsroom terms identify Samsung Electronics as samsung.com operator",
    "dewalt.co.uk": "DEWALT terms identify DEWALT Industrial Power Tool Company Limited as site operator",
    "einhell.co.uk": "Einhell UK company page identifies the UK subsidiary and its power-tool range",
    "stihl.co.uk": "Site terms identify Andreas Stihl Limited as the operator of stihl.co.uk",
}
SEEDS = tuple(replace(seed, evidence_excerpt=_EVIDENCE_EXCERPTS[seed.host]) for seed in SEEDS)

# The request category is an input contract, not a category inferred from a
# search snippet.  A seed is usable only within its reviewed product area.
_SEED_CATEGORIES = {
    "siemens-home.bsh-group.com": ("major appliances",),
    "haier-europe.com": ("major appliances",),
    "home-appliances.philips": ("small appliances",),
    "braunhousehold.com": ("small appliances",),
    "kenwoodworld.com": ("small appliances",),
    "bosch-professional.com": ("power tools",),
    "tp-link.com": ("networking",),
    "tp-link.cz": ("networking",),
    "netgear.com": ("networking",),
    "razer.com": ("computer/peripherals",),
    "corsair.com": ("PC components",),
    "us.roborock.com": ("small appliances",),
    "cerave.com": ("personal care/skincare",),
    "oralb.com": ("personal care/skincare",),
    "fishfrostbite.com": ("fishing",),
    "nautilusreels.com": ("fishing",),
    "bosch-home.co.uk": ("major appliances",),
    "electrolux.bg": ("major appliances",),
    "aeg.fr": ("major appliances",),
    "miele.co.uk": ("major appliances",),
    "asus.com": ("networking", "PC components"),
    "logitech.com": ("computer/peripherals",),
    "usa.canon.com": ("computer/peripherals",),
    "epson.eu": ("computer/peripherals",),
    "makita.co.nz": ("power tools",),
    "adidas.ae": ("apparel/footwear",),
    "nike.com": ("apparel/footwear",),
    "samsung.com": ("major appliances", "TV/display"),
    "dewalt.co.uk": ("power tools",),
    "einhell.co.uk": ("power tools",),
    "stihl.co.uk": ("garden/outdoor tools",),
}
SEEDS = tuple(replace(seed, categories=_SEED_CATEGORIES[seed.host]) for seed in SEEDS)


def find_host_seed_lead(brand: str, host: str, *, today: date | None = None) -> AuthoritySeed | None:
    """Find an audited host for page inspection; this grants no authority."""
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


def find_seed(brand: str, host: str, *, today: date | None = None,
              category: str = "unknown") -> AuthoritySeed | None:
    lead = find_host_seed_lead(brand, host, today=today)
    return lead if lead and category.casefold() in {item.casefold() for item in lead.categories} else None


def single_first_party_host_hint(brand: str, *, today: date | None = None) -> str | None:
    """One current audited host for a search hint; never an authority decision."""
    hosts = tuple(dict.fromkeys(
        seed.host for seed in SEEDS
        if seed.first_party and normalize_model(seed.brand) == normalize_model(brand)
        and find_host_seed_lead(brand, seed.host, today=today) is not None
    ))
    return hosts[0] if len(hosts) == 1 else None
