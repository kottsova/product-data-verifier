import unittest

from core.authority import evaluate_content_authority


class ContentAuthorityTests(unittest.TestCase):
    def test_brand_copyright_footer_on_brand_domain_is_verified(self):
        # Reproduces janome.club: domain label matches the brand and the
        # footer copyright names only the brand, with no store/retailer
        # qualifier.
        html = "<html><body><footer><p>© 2003-2026 Janome. All rights reserved.</p></footer></body></html>"
        text = "Janome Sakura 95 © 2003-2026 Janome. All rights reserved."
        evidence = evaluate_content_authority(html, text, "janome.club", "Janome")
        self.assertTrue(evidence.verified)
        self.assertEqual(evidence.signal, "copyright_entity")

    def test_retailer_copyright_naming_the_store_not_the_brand_is_not_verified(self):
        # Reproduces gressel.ru: domain label equals the brand, but the
        # site's own copyright names itself as an "online store", not the
        # brand alone, so it must not be classified as manufacturer content.
        html = "<html><body><footer><p>2026 © gressel.ru - online store</p></footer></body></html>"
        text = "2026 © gressel.ru - online store"
        evidence = evaluate_content_authority(html, text, "gressel.ru", "Gressel")
        self.assertFalse(evidence.verified)

    def test_brand_domain_consistency_alone_is_not_sufficient(self):
        html = "<html><body><footer><p>© 2024 Some Other Company</p></footer></body></html>"
        text = "© 2024 Some Other Company"
        evidence = evaluate_content_authority(html, text, "janome.club", "Janome")
        self.assertFalse(evidence.verified)

    def test_content_evidence_without_domain_consistency_is_not_sufficient(self):
        # A multi-brand retailer mentioning the brand extensively in its own
        # copyright must not be verified when the domain itself does not
        # name the brand.
        html = "<html><body><footer><p>© 2024 Sewing World</p></footer></body></html>"
        text = "Janome Bernina Pfaff © 2024 Sewing World"
        evidence = evaluate_content_authority(html, text, "sewing-world.ru", "Janome")
        self.assertFalse(evidence.verified)

    def test_structured_organization_data_is_verified(self):
        html = (
            '<html><head><script type="application/ld+json">'
            '{"@type": "Organization", "name": "Gressel"}'
            "</script></head><body></body></html>"
        )
        evidence = evaluate_content_authority(html, "", "gressel.ru", "Gressel")
        self.assertTrue(evidence.verified)
        self.assertEqual(evidence.signal, "structured_data_organization")

    def test_structured_product_brand_data_is_verified(self):
        html = (
            '<html><head><script type="application/ld+json">'
            '{"@type": "Product", "name": "GAF-1825", "brand": {"@type": "Brand", "name": "Gressel"}}'
            "</script></head><body></body></html>"
        )
        evidence = evaluate_content_authority(html, "", "gressel.ru", "Gressel")
        self.assertTrue(evidence.verified)
        self.assertEqual(evidence.signal, "structured_data_product_brand")

    def test_site_name_meta_is_verified(self):
        html = '<html><head><meta property="og:site_name" content="Janome"></head><body></body></html>'
        evidence = evaluate_content_authority(html, "", "janome.club", "Janome")
        self.assertTrue(evidence.verified)
        self.assertEqual(evidence.signal, "site_name_meta")

    def test_corporate_suffix_is_stripped_before_comparison(self):
        html = "<html><body><footer><p>© 2024 Bosch GmbH. All rights reserved.</p></footer></body></html>"
        text = "© 2024 Bosch GmbH. All rights reserved."
        evidence = evaluate_content_authority(html, text, "bosch.com", "Bosch")
        self.assertTrue(evidence.verified)

    def test_unrelated_third_party_trademark_mention_is_not_verified(self):
        # A generic retailer must not become "verified" just because a page
        # about the requested brand happens to live on it.
        html = "<html><body><footer><p>© 2024 DNS Shop</p></footer></body></html>"
        text = "Janome Sakura 95 sewing machine © 2024 DNS Shop"
        evidence = evaluate_content_authority(html, text, "dns-shop.ru", "Janome")
        self.assertFalse(evidence.verified)

    def test_missing_brand_or_domain_is_not_verified(self):
        self.assertFalse(evaluate_content_authority("<html></html>", "", "", "Janome").verified)
        self.assertFalse(evaluate_content_authority("<html></html>", "", "janome.club", "").verified)


if __name__ == "__main__":
    unittest.main()
