import unittest

from core.authority import TrustedSource, resolve_authority


class ResolveAuthorityAdversarialTests(unittest.TestCase):
    """Stage 18.5 follow-up: self-assertion alone must never reach an
    elevated role (manufacturer/official_distributor/authorized_dealer).
    Elevation requires independent cross-domain corroboration from an
    already-trusted source in the same run.
    """

    def test_1_brand_named_retailer_with_matching_copyright_is_not_elevated(self):
        # Reproduces janome.club: brand-consistent domain, copyright names
        # only the brand, but nothing independently corroborates it.
        html = "<html><body><footer>© 2003-2026 Janome. All rights reserved.</footer></body></html>"
        text = "© 2003-2026 Janome. All rights reserved."
        result = resolve_authority(html, text, "janome.club", "Janome", trusted_sources=())
        self.assertEqual(result.role, "unknown")
        self.assertEqual(result.self_declared.role, "manufacturer")
        self.assertFalse(result.corroboration.found)

    def test_2_unofficial_dealer_claiming_official_is_not_elevated(self):
        html = "<html><body><p>Janome Shop - official authorized dealer of Janome sewing machines</p></body></html>"
        text = "Janome Shop - official authorized dealer of Janome sewing machines"
        result = resolve_authority(html, text, "janome-shop.example", "Janome", trusted_sources=())
        self.assertEqual(result.role, "unknown")
        self.assertEqual(result.self_declared.role, "authorized_dealer")
        self.assertFalse(result.corroboration.found)

    def test_3_retailer_with_matching_json_ld_is_not_elevated(self):
        html = (
            '<html><head><script type="application/ld+json">'
            '{"@type": "Organization", "name": "Janome"}'
            "</script></head><body></body></html>"
        )
        result = resolve_authority(html, "", "janome.club", "Janome", trusted_sources=())
        self.assertEqual(result.role, "unknown")
        self.assertFalse(result.corroboration.found)

    def test_4_marketplace_seller_is_never_elevated_regardless_of_claims(self):
        html = "<html><body><footer>© 2024 Janome Official Store</footer></body></html>"
        text = "official authorized dealer Janome © 2024 Janome Official Store"
        result = resolve_authority(html, text, "ozon.ru", "Janome", trusted_sources=())
        self.assertEqual(result.role, "marketplace")
        self.assertFalse(result.corroboration.found)

    def test_5_manufacturer_domain_without_corroboration_still_uncorroborated(self):
        # Even the strongest single-page self-declared signal (brand-only
        # copyright) must not become "manufacturer" without corroboration -
        # the pre-existing SERP-verified path is the only bypass, and it is
        # a separate mechanism this function does not see.
        html = "<html><head><script type='application/ld+json'>{\"@type\":\"Organization\",\"name\":\"Acme\"}</script></head><body><footer>© 2024 Acme</footer></body></html>"
        text = "© 2024 Acme"
        result = resolve_authority(html, text, "acme.example", "Acme", trusted_sources=())
        self.assertEqual(result.self_declared.role, "manufacturer")
        self.assertEqual(result.role, "unknown")

    def test_6_independently_corroborated_authorized_dealer_is_elevated(self):
        html = "<html><body><p>Acme Shop is an authorized dealer of Acme products.</p></body></html>"
        text = "Acme Shop is an authorized dealer of Acme products."
        anchor_html = '<html><body><a href="https://acme-shop.example/store">Authorized dealer</a></body></html>'
        trusted = (TrustedSource(domain="acme.example", html=anchor_html),)
        result = resolve_authority(html, text, "acme-shop.example", "Acme", trusted_sources=trusted)
        self.assertEqual(result.role, "authorized_dealer")
        self.assertTrue(result.corroboration.found)
        self.assertEqual(result.corroboration.evidence_domain, "acme.example")

    def test_7_independently_corroborated_official_distributor_is_elevated(self):
        html = "<html><body><p>Acme Import LLC - official distributor of Acme in this region.</p></body></html>"
        text = "Acme Import LLC - official distributor of Acme in this region."
        anchor_html = '<html><body><a href="https://acme-import.example/">Official distributor</a></body></html>'
        trusted = (TrustedSource(domain="acme.example", html=anchor_html),)
        result = resolve_authority(html, text, "acme-import.example", "Acme", trusted_sources=trusted)
        self.assertEqual(result.role, "official_distributor")
        self.assertTrue(result.corroboration.found)

    def test_no_self_declared_signal_at_all_is_unknown(self):
        html = "<html><body><p>Nothing brand-related here.</p></body></html>"
        result = resolve_authority(html, "Nothing brand-related here.", "janome.club", "Janome", trusted_sources=())
        self.assertEqual(result.role, "unknown")
        self.assertEqual(result.self_declared.role, "unknown")

    def test_brand_domain_inconsistency_stays_unknown_even_with_content_claims(self):
        html = "<html><body><p>Official authorized dealer of Janome</p><footer>© 2024 Janome</footer></body></html>"
        text = "Official authorized dealer of Janome © 2024 Janome"
        result = resolve_authority(html, text, "sewing-world.ru", "Janome", trusted_sources=())
        self.assertEqual(result.role, "unknown")

    def test_official_supply_wording_variant_is_a_distributor_claim(self):
        # "official supplies to <country>" is a common generic Russian
        # e-commerce self-declaration distinct from the literal phrase
        # "official distributor" - must still be recognized as a claim
        # (and, exactly like any other self-declared claim, still requires
        # corroboration before it can be elevated).
        html = "<html><body><p>Официальные поставки в Россию</p></body></html>"
        text = "Официальные поставки в Россию"
        result = resolve_authority(html, text, "acme-shop.example", "Acme", trusted_sources=())
        self.assertEqual(result.self_declared.role, "official_distributor")
        self.assertEqual(result.role, "unknown")

    def test_corroboration_requires_matching_domain_not_just_any_link(self):
        html = "<html><body><p>Acme Shop is an authorized dealer of Acme products.</p></body></html>"
        text = "Acme Shop is an authorized dealer of Acme products."
        anchor_html = '<html><body><a href="https://unrelated.example/">Somewhere else</a></body></html>'
        trusted = (TrustedSource(domain="acme.example", html=anchor_html),)
        result = resolve_authority(html, text, "acme-shop.example", "Acme", trusted_sources=trusted)
        self.assertEqual(result.role, "unknown")
        self.assertFalse(result.corroboration.found)


if __name__ == "__main__":
    unittest.main()
