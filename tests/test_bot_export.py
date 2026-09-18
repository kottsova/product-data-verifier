"""Stage 30: bot/export.py -- the /export CSV built from the stable service
DTOs (ServiceAttribute/ServiceEvidence), never from FinalProductProfile."""

from __future__ import annotations

import csv
from io import StringIO
import unittest

from bot.export import build_export_rows, export_result_csv
from services.product_verifier import (
    ServiceAttribute,
    ServiceCategory,
    ServiceEvidence,
    ServiceIdentity,
    VerifyProductRequest,
    VerifyProductResult,
)


def _identity(**overrides):
    defaults = dict(
        brand="Acme", base_model="X100", commercial_model="X100",
        manufacturer_article=None, product_code=None, sku=None, gtin=None,
        color=None, configuration={}, confidence="high",
    )
    defaults.update(overrides)
    return ServiceIdentity(**defaults)


_MISSING = object()


def _result(*, attributes=(), identity=_MISSING, category=_MISSING) -> VerifyProductResult:
    return VerifyProductResult(
        success=True,
        request=VerifyProductRequest(brand="Acme", model="X100"),
        identity=_identity() if identity is _MISSING else identity,
        category=(
            ServiceCategory(
                category_id="cooktop", category_name="Cooktop",
                parent_category="major_appliance", confidence="high",
            )
            if category is _MISSING else category
        ),
        attributes=attributes,
    )


class BuildExportRowsTests(unittest.TestCase):
    def test_confirmed_attribute_with_supporting_sources_yields_one_row_per_source(self):
        attribute = ServiceAttribute(
            canonical_name="power", display_name="Power", value="1000", unit="W",
            status="Confirmed", confidence="high", source="https://brand.example/power",
            evidence="power: 1000 W", priority="high", expected=True, discovered=False,
            supporting_sources=(
                ServiceEvidence(
                    value="1000", unit="W", source="https://brand.example/power",
                    source_type="manufacturer", evidence="power: 1000 W",
                    authority_status="verified", confidence="high", origin="fetch",
                ),
                ServiceEvidence(
                    value="1000", unit="W", source="https://shop.example/power",
                    source_type="retailer", evidence="power: 1000 W",
                    authority_status="unverified", confidence="medium", origin="fetch",
                ),
            ),
        )
        rows = build_export_rows(_result(attributes=(attribute,)))
        self.assertEqual(len(rows), 2)
        self.assertEqual({row["source"] for row in rows}, {
            "https://brand.example/power", "https://shop.example/power",
        })

    def test_attribute_without_supporting_sources_still_yields_one_row(self):
        attribute = ServiceAttribute(
            canonical_name="power", display_name="Power", value="1000", unit="W",
            status="Confirmed", confidence="high", source="https://brand.example/power",
            evidence="power: 1000 W", priority="high", expected=True, discovered=False,
        )
        rows = build_export_rows(_result(attributes=(attribute,)))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source"], "https://brand.example/power")

    def test_value_text_uses_the_same_formatting_as_telegram_messages(self):
        attribute = ServiceAttribute(
            canonical_name="power", display_name="Power", value="1000", unit="W",
            status="Confirmed", confidence="high", source="https://brand.example/power",
            evidence="power: 1000 W", priority="high", expected=True, discovered=False,
        )
        rows = build_export_rows(_result(attributes=(attribute,)))
        self.assertEqual(rows[0]["value_text"], "1000 W")


class ExportResultCsvTests(unittest.TestCase):
    def _confirmed_attribute(self):
        return ServiceAttribute(
            canonical_name="power", display_name="Power", value="1000", unit="W",
            status="Confirmed", confidence="high", source="https://brand.example/power",
            evidence="power: 1000 W", priority="high", expected=True, discovered=False,
        )

    def test_csv_contains_brand_model_category(self):
        result = _result(attributes=(self._confirmed_attribute(),))
        rows = list(csv.DictReader(StringIO(export_result_csv(result))))
        self.assertEqual(rows[0]["Brand"], "Acme")
        self.assertEqual(rows[0]["Model"], "X100")
        self.assertEqual(rows[0]["Category"], "Cooktop")

    def test_falls_back_to_request_brand_model_when_identity_is_missing(self):
        result = _result(attributes=(self._confirmed_attribute(),), identity=None, category=None)
        rows = list(csv.DictReader(StringIO(export_result_csv(result))))
        self.assertEqual(rows[0]["Brand"], "Acme")
        self.assertEqual(rows[0]["Model"], "X100")
        self.assertEqual(rows[0]["Category"], "")

    def test_export_is_deterministic(self):
        result = _result(attributes=(self._confirmed_attribute(),))
        self.assertEqual(export_result_csv(result), export_result_csv(result))


if __name__ == "__main__":
    unittest.main()
