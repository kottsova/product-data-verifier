"""Stage 33.2: metadata locations, SKU rejection records, unknown-purpose PDFs, performance."""

from __future__ import annotations

import unittest

from core.discovery import DiscoveryOutcome, ProviderAttempt
from core.page_inspection import inspect_product_page
from services.discovery_debug import (
    DiscoverySource,
    _document_from_candidate,
    _performance,
    _sku_rejections,
    locale_region,
)
from core.official_documents import RejectedDocument


class Stage332Tests(unittest.TestCase):
    def test_unknown_purpose_pdf_is_not_a_document(self) -> None:
        source = DiscoverySource(
            "https://brand.com/files/LCA_Results.pdf", "brand.com", "official_document",
            "LCA Results", "exact", "official support", "x", "official", page_role="document",
        )
        document, rejection = _document_from_candidate(source, "Brand", "SM-S921B")
        self.assertIsNone(document)
        self.assertIsNotNone(rejection)

    def test_hidden_dom_location(self) -> None:
        html = (
            "<html><body><h1>Product</h1><button>Specifications</button>"
            "<div class='specs' hidden><table>"
            + "".join(f"<tr><th>k{i}</th><td>v{i}</td></tr>" for i in range(5))
            + "</table></div><p>" + "text " * 200 + "</p></body></html>"
        )
        result = inspect_product_page(html, "https://x.com/p")
        self.assertEqual(result.requires_interaction, "false")
        self.assertEqual(result.spec_location, "dom_hidden")

    def test_sku_rejection_records_found_and_requested(self) -> None:
        near = DiscoverySource(
            "https://brand.com/p/dcd796d2-gb", "brand.com", "manufacturer", "DCD796D2",
            "rejected", "manufacturer", "different commercial-model variant", "rejected",
        )
        records = _sku_rejections("DCD796P2", [near], [RejectedDocument("https://a.com/x.pdf", "manual", "", "r")])
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["requested_sku"], "DCD796P2")
        self.assertIn("796", records[0]["found_sku"])

    def test_locale_region_and_performance(self) -> None:
        self.assertEqual(locale_region("https://www.dewalt.co.uk/en-gb/product/x"), "en-GB")
        self.assertEqual(locale_region("https://gressel.ru/catalog/x/"), ".ru")
        outcome = DiscoveryOutcome(
            attempted_queries=["a", "b"],
            provider_attempts=[
                ProviderAttempt("p", "a", "blocked"), ProviderAttempt("p", "b", "circuit_open"),
            ],
        )
        perf = _performance(outcome, {"provider_raw": 4}, 31.0)
        self.assertEqual((perf["query_count"], perf["blocked"], perf["circuit_open"]), (2, 1, 1))
        self.assertEqual(perf["slow"], "over_30s")

    def test_dotted_identifier_in_url_is_the_same_model(self) -> None:
        from core.discovery import _brand_evidence, _path_names_complete_model
        from core.discovery import SearchResultRecord
        from core.match import candidate_model_match

        url = "https://www.brand.com/p/machine-ec685.m/EC685.M.html"
        self.assertEqual(candidate_model_match("EC685M", "Machine EC685.M", url), "exact")
        self.assertTrue(_path_names_complete_model(url, "Brand", "EC685M"))
        # a different dotted suffix stays a different product
        self.assertEqual(
            candidate_model_match("EC685M", "x", "https://www.brand.com/p/ec685.r/EC685.R.html"), "mismatch",
        )
        record = SearchResultRecord("https://logitech.com/en-us/shop/p/mx-master-3s", "/en us/shop")
        self.assertTrue(_brand_evidence(record, "Logitech"))
        other = SearchResultRecord("https://logitech-fans.com/p/mx-master-3s", "/p")
        self.assertFalse(_brand_evidence(other, "Logitech"))


if __name__ == "__main__":
    unittest.main()
