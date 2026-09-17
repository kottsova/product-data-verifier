import json
from pathlib import Path
import unittest

from diagnostics.retrieval_conversion_audit import audit_artifact


ROOT = Path(__file__).resolve().parents[1]


class RetrievalConversionAuditTests(unittest.TestCase):
    def test_stage23_dataset_is_the_frozen_stage22_set(self) -> None:
        dataset = json.loads(
            (ROOT / "regression" / "datasets" / "stage23_blind_v1.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(len(dataset["products"]), 15)
        self.assertEqual(len({item["id"] for item in dataset["products"]}), 15)
        self.assertIn("blind-smartphone-google-pixel9pro", {
            item["id"] for item in dataset["products"]
        })

    def test_funnel_uses_exact_official_source_provenance(self) -> None:
        url = "https://acme.example/support/X100"
        artifact = {
            "products": [{
                "id": "x", "expected_category": "smartphone",
            }],
            "records": [{
                "product_id": "x", "runtime_seconds": 4.0, "timed_out": False,
                "trace": {
                    "discovery": {
                        "accepted_candidates": [{
                            "url": url, "source_type": "official_document",
                            "authority_status": "verified", "model_match": "exact",
                            "model_relevance": "exact_base_model",
                            "identity_relation": "same_base_model",
                        }],
                        "rejected_candidates": [],
                    },
                    "relevance_and_ranking": {"selected": [{"url": url}]},
                    "fetch": {"sources": [{
                        "url": url, "final_url": url, "status": "success",
                    }]},
                    "extraction": {"attributes": [{"source_url": url}]},
                    "category_and_schema": {"category": "smartphone"},
                    "mapping": {
                        "mapped": [{"canonical_name": "brand", "source_url": url}],
                        "derived": [],
                    },
                    "validation": {"facts": [{
                        "canonical_name": "brand", "status": "Confirmed",
                        "authority_status": "verified",
                        "identity_relation": "same_base_model",
                    }]},
                    "quality": {
                        "confirmed_count": 1, "unresolved_count": 16,
                        "conflict_count": 0, "coverage_percent": 5.9,
                        "status": "insufficient",
                    },
                    "budget": {"exhausted": False},
                },
            }],
        }
        result = audit_artifact(artifact)
        funnel = result["aggregate"]["funnel"]
        self.assertTrue(all(value == 1 for value in funnel.values()))
        self.assertEqual(
            result["aggregate"]["conversion"],
            {
                "exact_official_discovered_to_fetched": 1.0,
                "fetched_official_to_raw": 1.0,
                "raw_to_expected_mapped": 1.0,
                "expected_mapped_to_confirmed": 1.0,
            },
        )


if __name__ == "__main__":
    unittest.main()
