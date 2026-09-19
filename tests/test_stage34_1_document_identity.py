"""Stage 34.1: official documents are exact only with identity evidence inside the document."""

from __future__ import annotations

import unittest

from core.document_identity import DocumentText, verify_document_identity, verify_documents
from core.official_documents import OfficialDocument, documents_by_canonical_field
from services.discovery_debug import _verify_document_identity


def doc(url: str, match: str = "exact", doc_type: str = "declaration") -> OfficialDocument:
    return OfficialDocument(
        url=url, doc_type=doc_type, canonical_field="declaration_url" if doc_type == "declaration" else "manual_url",
        title="t", authority="official", model_match=match, reason="linked from the exact official product page",
        file_type="pdf",
    )


def text(*pages: str) -> DocumentText:
    return DocumentText(tuple(pages))


class Verdicts(unittest.TestCase):
    def test_document_naming_the_sku_is_exact(self):
        verdict = verify_document_identity("HBG7741B1", "Bosch", text("EU declaration\nModel: HBG7741B1"))
        self.assertEqual((verdict.match, verdict.evidence), ("exact", "document_text"))

    def test_slash_and_underscore_spellings_are_the_same_identifier(self):
        self.assertEqual(verify_document_identity("HX9992/12", "Philips", text("Type HX9992_12")).match, "exact")

    def test_base_sku_with_regional_requested_suffix_is_exact(self):
        verdict = verify_document_identity("HX9992/12", "Philips", text("Philips HX9990, HX9992, HX9993"))
        self.assertEqual((verdict.match, verdict.evidence), ("exact", "document_text"))

    def test_base_sku_with_variant_requested_suffix_is_only_probable(self):
        verdict = verify_document_identity("ABC1234/KIT", "B", text("ABC1234 datasheet"))
        self.assertEqual(verdict.match, "probable")

    def test_neighbouring_internal_identifier_is_not_identity(self):
        verdict = verify_document_identity("HBG7741B1", "Bosch", text("Declaration No. HT6B60F0ShB\nOvens with Home Connect"))
        self.assertEqual(verdict.match, "unverified")

    def test_generic_manual_without_sku_or_name_is_unverified(self):
        verdict = verify_document_identity(
            "Sonicare 9900 Prestige HX9992/12", "Philips", text("Your toothbrush\nHX9200 charger\nSafety"),
        )
        self.assertEqual(verdict.match, "unverified")

    def test_family_words_without_sku_are_probable_not_exact(self):
        verdict = verify_document_identity(
            "Sonicare 9900 Prestige HX9992/12", "Philips", text("Philips Sonicare 9900 Prestige user manual"),
        )
        self.assertEqual((verdict.match, verdict.evidence), ("probable", "family_words"))

    def test_model_stem_is_family_evidence(self):
        verdict = verify_document_identity("EC685M", "DeLonghi", text("EC685 EC695 EC785 coffee machine"))
        self.assertEqual((verdict.match, verdict.evidence), ("probable", "family_stem"))

    def test_different_variant_only_is_unverified(self):
        verdict = verify_document_identity("DCD796P2", "DEWALT", text("DCD796P2T kit"))
        self.assertEqual(verdict.match, "unverified")

    def test_unreadable_documents_cannot_prove_identity(self):
        for status in ("fetch_failed", "unreadable", "no_text_layer"):
            self.assertEqual(verify_document_identity("X1234", "B", DocumentText(status=status)).match, "unverified")


class Wiring(unittest.TestCase):
    def test_only_exact_pdfs_are_read_and_unverified_is_kept_but_not_canonical(self):
        good, bad, page = doc("https://o.example/good.pdf"), doc("https://o.example/bad.pdf"), doc("https://o.example/page")
        reads: list[str] = []
        texts = {good.url: text("HBG7741B1"), bad.url: text("HT6B60F0S")}

        def reader(url: str) -> DocumentText:
            reads.append(url)
            return texts[url]

        checked = _verify_document_identity([good, bad, page], "Bosch", "HBG7741B1", reader)
        by_url = {item.url: item for item in checked}
        self.assertEqual(sorted(reads), [bad.url, good.url])  # the HTML page is not read
        self.assertEqual(by_url[good.url].model_match, "exact")
        self.assertEqual(by_url[bad.url].model_match, "unverified")
        self.assertIn("identity check", by_url[bad.url].reason)
        self.assertEqual(by_url[page.url].model_match, "exact")
        self.assertEqual([item.url for item in checked][-1], bad.url)  # unverified sorts last
        self.assertNotIn(bad.url, documents_by_canonical_field(checked)["declaration_url"])
        self.assertEqual(verify_documents([doc("https://o.example/p", "probable")], "M1234", "B", reader), {})


if __name__ == "__main__":
    unittest.main()
