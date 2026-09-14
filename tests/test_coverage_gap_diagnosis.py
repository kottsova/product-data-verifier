"""Deterministic unit tests for Stage 8.3 coverage_gap_diagnosis.

All tests use SimpleNamespace mock fixtures. No live network access.
"""

from __future__ import annotations

import json
import sys
import unittest
from io import StringIO
from types import SimpleNamespace
from unittest.mock import patch

from diagnostics.coverage_gap_diagnosis import (
    CHAIN_KEYS,
    LOSS_CLASSES,
    build_chain,
    classify_loss,
    diagnose_result,
)
from diagnostics.attribute_coverage_audit import AuditProduct


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

def _discovery(
    *,
    candidates=None,
    rejected_candidates=None,
    search_status="success",
    issues=None,
):
    return SimpleNamespace(
        candidates=list(candidates or []),
        rejected_candidates=list(rejected_candidates or []),
        search_status=search_status,
        issues=list(issues or []),
    )


def _candidate(url="https://example.com/product/x"):
    return {"url": url, "relevance_relation": "exact"}


def _fetch(url="https://example.com/product/x", *, status="success", blocked_reason=None):
    return {
        "source_url": url,
        "final_url": url,
        "status": status,
        "blocked_reason": blocked_reason,
        "source_type": "manufacturer",
        "authority_status": "verified",
        "identity_relation": "same_base_model",
    }


def _raw(name, *, source_url="https://example.com/product/x"):
    """Minimal RawAttribute-like object."""
    return SimpleNamespace(
        name=name,
        value="some value",
        unit=None,
        source_url=source_url,
        source_type="manufacturer",
        evidence=f"{name}: some value",
        extraction_method="html_table",
        confidence="high",
        raw_value="some value",
        attribute_kind="product",
        context=None,
    )


def _canonical_attr(canonical_name):
    """Minimal CanonicalAttribute-like object."""
    return SimpleNamespace(canonical_name=canonical_name)


def _mapping(*, mapped=None, unmapped=None, ambiguous=None):
    return SimpleNamespace(
        canonical_attributes=list(mapped or []),
        unmapped=list(unmapped or []),
        ambiguous=list(ambiguous or []),
    )


def _ambiguous_mapping(raw_name):
    """Minimal AmbiguousMapping-like object."""
    return SimpleNamespace(raw_attribute=SimpleNamespace(name=raw_name))


def _profile_attr(canonical_name, *, status="Confirmed"):
    return SimpleNamespace(canonical_name=canonical_name, status=status)


def _final_profile(attrs=None):
    items = list(attrs or [])
    return SimpleNamespace(
        attributes=items,
        by_name={item.canonical_name: item for item in items},
        conflicts=[i for i in items if i.status == "Conflict"],
    )


def _category(category_id="cooktop"):
    return SimpleNamespace(category_id=category_id)


def _make_result(
    *,
    candidates=None,
    rejected_candidates=None,
    discovery_status="success",
    discovery_issues=None,
    selected=None,
    fetched=None,
    raw_attrs=None,
    mapped=None,
    unmapped=None,
    ambiguous=None,
    targeted_fields=None,
    profile_attrs=None,
    category_id="cooktop",
):
    targeted_search = (
        SimpleNamespace(fields=list(targeted_fields))
        if targeted_fields is not None
        else None
    )
    return SimpleNamespace(
        discovery=_discovery(
            candidates=candidates,
            rejected_candidates=rejected_candidates,
            search_status=discovery_status,
            issues=discovery_issues,
        ),
        selected_candidates=tuple(selected or []),
        fetched_sources=tuple(fetched or []),
        raw_attributes=tuple(raw_attrs or []),
        mapping=_mapping(mapped=mapped, unmapped=unmapped, ambiguous=ambiguous),
        targeted_search=targeted_search,
        final_profile=_final_profile(profile_attrs),
        category=_category(category_id),
    )


def _targeted_field(
    canonical_name,
    *,
    qr=None,
    search_status="success",
    absence_evidence=None,
):
    """Build a TargetedFieldResult-like object with one or zero query results."""
    query_results = list(qr or [])
    return SimpleNamespace(
        gap=SimpleNamespace(canonical_name=canonical_name),
        query_results=query_results,
        search_status=search_status,
        absence_evidence=tuple(absence_evidence or ()),
    )


def _query_result(
    *,
    fetched=None,
    extracted=None,
    mapped=None,
    candidates=None,
    rejected=None,
    absence_evidence=None,
):
    return SimpleNamespace(
        fetched_sources=list(fetched or []),
        extracted_relevant_facts=list(extracted or []),
        mapped_candidate_facts=list(mapped or []),
        discovery_candidates=list(candidates or []),
        rejected_candidates=list(rejected or []),
        absence_evidence=tuple(absence_evidence or ()),
    )


# ---------------------------------------------------------------------------
# Product used in diagnose_result tests
# ---------------------------------------------------------------------------

AUDIT_PRODUCT = AuditProduct("Acme", "X100", "cooktop")


# ---------------------------------------------------------------------------
# Tests – classify_loss
# ---------------------------------------------------------------------------

class TestClassifyLossSourceNotDiscovered(unittest.TestCase):
    def test_returns_source_not_discovered_when_no_candidates(self):
        result = _make_result(discovery_status="blocked")
        loss_class, detail, chain = classify_loss(result, "number_of_zones", "cooktop")
        self.assertEqual(loss_class, "source_not_discovered")
        self.assertIn("discovery_status=blocked", detail)
        self.assertFalse(chain["source_candidate_exists"])


class TestClassifyLossSourceRejected(unittest.TestCase):
    def test_returns_source_rejected_when_all_rejected_by_gate(self):
        result = _make_result(
            candidates=[],
            rejected_candidates=[_candidate()],
        )
        loss_class, detail, chain = classify_loss(result, "number_of_zones", "cooktop")
        self.assertEqual(loss_class, "source_rejected")
        self.assertIn("relevance gate", detail)
        self.assertTrue(chain["source_candidate_exists"])
        self.assertFalse(chain["source_selected"])


class TestClassifyLossSourceNotSelected(unittest.TestCase):
    def test_returns_source_not_selected_when_candidates_exist_but_none_selected(self):
        result = _make_result(
            candidates=[_candidate()],
            selected=[],
        )
        loss_class, detail, chain = classify_loss(result, "number_of_zones", "cooktop")
        self.assertEqual(loss_class, "source_not_selected")
        self.assertFalse(chain["source_selected"])


class TestClassifyLossFetchBlocked(unittest.TestCase):
    def test_returns_fetch_blocked_when_blocked_reason_set(self):
        result = _make_result(
            candidates=[_candidate()],
            selected=[_candidate()],
            fetched=[_fetch(status="blocked", blocked_reason="HTTP 403")],
        )
        loss_class, detail, chain = classify_loss(result, "number_of_zones", "cooktop")
        self.assertEqual(loss_class, "fetch_blocked")
        self.assertFalse(chain["source_fetched_ok"])

    def test_returns_fetch_blocked_when_status_blocked(self):
        result = _make_result(
            candidates=[_candidate()],
            selected=[_candidate()],
            fetched=[_fetch(status="blocked")],
        )
        loss_class, _, chain = classify_loss(result, "number_of_zones", "cooktop")
        self.assertEqual(loss_class, "fetch_blocked")


class TestClassifyLossFetchError(unittest.TestCase):
    def test_returns_fetch_error_when_all_fetch_errors_no_block(self):
        result = _make_result(
            candidates=[_candidate()],
            selected=[_candidate()],
            fetched=[_fetch(status="error")],
        )
        loss_class, detail, chain = classify_loss(result, "number_of_zones", "cooktop")
        self.assertEqual(loss_class, "fetch_error")
        self.assertFalse(chain["source_fetched_ok"])
        self.assertIn("network", detail)


class TestClassifyLossExtractionMissed(unittest.TestCase):
    def test_returns_extraction_missed_when_fetch_ok_no_raw(self):
        result = _make_result(
            candidates=[_candidate()],
            selected=[_candidate()],
            fetched=[_fetch(status="success")],
            raw_attrs=[],  # no raw attributes at all
        )
        # Use a canonical_name that has no targeted search field
        loss_class, detail, chain = classify_loss(result, "number_of_zones", "cooktop")
        self.assertEqual(loss_class, "extraction_missed")
        self.assertTrue(chain["source_fetched_ok"])
        self.assertFalse(chain["raw_attr_extracted"])
        self.assertIn("successful", detail)


class TestClassifyLossMappingMissed(unittest.TestCase):
    def test_returns_mapping_missed_when_raw_alias_present_but_unmapped(self):
        # "number of zones" is an alias for number_of_zones in cooktop schema
        result = _make_result(
            candidates=[_candidate()],
            selected=[_candidate()],
            fetched=[_fetch(status="success")],
            raw_attrs=[_raw("number of zones")],
            unmapped=[_raw("number of zones")],  # in unmapped list
        )
        loss_class, detail, chain = classify_loss(result, "number_of_zones", "cooktop")
        self.assertEqual(loss_class, "mapping_missed")
        self.assertTrue(chain["raw_attr_extracted"])
        self.assertFalse(chain["canonical_mapped"])
        self.assertIn("unmapped", detail)

    def test_returns_mapping_missed_when_ambiguous(self):
        result = _make_result(
            candidates=[_candidate()],
            selected=[_candidate()],
            fetched=[_fetch(status="success")],
            raw_attrs=[_raw("number of zones")],
            ambiguous=[_ambiguous_mapping("number of zones")],
        )
        loss_class, detail, chain = classify_loss(result, "number_of_zones", "cooktop")
        self.assertEqual(loss_class, "mapping_missed")
        self.assertIn("ambiguous", detail)


class TestClassifyLossValidationUnresolved(unittest.TestCase):
    def test_returns_validation_unresolved_when_mapped_but_profile_unresolved(self):
        # Simulate edge case: raw alias present, canonical_mapped=True, but
        # the attribute is in final_profile as Unresolved.
        # (In normal flow schema_missing wouldn't include a mapped name, but
        # this tests the robustness branch.)
        result = _make_result(
            candidates=[_candidate()],
            selected=[_candidate()],
            fetched=[_fetch(status="success")],
            raw_attrs=[_raw("number of zones")],
            mapped=[_canonical_attr("number_of_zones")],
            profile_attrs=[_profile_attr("number_of_zones", status="Unresolved")],
        )
        loss_class, detail, chain = classify_loss(result, "number_of_zones", "cooktop")
        self.assertEqual(loss_class, "validation_unresolved")
        self.assertIn("Unresolved", detail)


class TestClassifyLossGenuinelyAbsent(unittest.TestCase):
    def test_empty_targeted_extraction_does_not_claim_genuine_absence(self):
        qr = _query_result(
            fetched=[_fetch(status="success")],
            extracted=[],
            mapped=[],
        )
        tf = _targeted_field("number_of_zones", qr=[qr], search_status="unresolved")
        result = _make_result(
            candidates=[_candidate()],
            selected=[_candidate()],
            fetched=[_fetch(status="success")],
            raw_attrs=[],
            targeted_fields=[tf],
        )
        loss_class, detail, chain = classify_loss(result, "number_of_zones", "cooktop")
        self.assertEqual(loss_class, "extraction_missed")
        self.assertTrue(chain["targeted_fetched_ok"])
        self.assertFalse(chain["targeted_extracted"])
        self.assertIn("absence is not proved", detail)

    def test_explicit_absence_evidence_is_required(self):
        qr = _query_result(
            fetched=[_fetch(status="success")],
            absence_evidence=["Manual: reverse function is not available."],
        )
        tf = _targeted_field("number_of_zones", qr=[qr], search_status="unresolved")
        result = _make_result(
            candidates=[_candidate()],
            selected=[_candidate()],
            fetched=[_fetch(status="success")],
            targeted_fields=[tf],
        )
        loss_class, detail, _ = classify_loss(result, "number_of_zones", "cooktop")
        self.assertEqual(loss_class, "genuinely_absent")
        self.assertIn("explicit absence evidence", detail)


class TestClassifyLossUnknown(unittest.TestCase):
    def test_returns_unknown_when_targeted_mapped_but_not_in_profile(self):
        # targeted_mapped=True, but not in final_profile and not Unresolved
        qr = _query_result(
            fetched=[_fetch(status="success")],
            extracted=[_raw("number of zones")],
            mapped=[_canonical_attr("number_of_zones")],
        )
        tf = _targeted_field("number_of_zones", qr=[qr], search_status="success")
        result = _make_result(
            candidates=[_candidate()],
            selected=[_candidate()],
            fetched=[_fetch(status="success")],
            raw_attrs=[],
            targeted_fields=[tf],
            profile_attrs=[],  # NOT in final profile
        )
        loss_class, _, chain = classify_loss(result, "number_of_zones", "cooktop")
        self.assertEqual(loss_class, "unknown")


class TestTargetedPathIndependence(unittest.TestCase):
    def test_targeted_success_is_recorded_when_initial_discovery_found_nothing(self):
        qr = _query_result(
            fetched=[_fetch(status="success")],
            extracted=[_raw("number of zones")],
            mapped=[_canonical_attr("number_of_zones")],
        )
        result = _make_result(
            candidates=[],
            selected=[],
            fetched=[],
            targeted_fields=[_targeted_field("number_of_zones", qr=[qr])],
            profile_attrs=[_profile_attr("number_of_zones", status="Unresolved")],
        )
        loss_class, _, chain = classify_loss(result, "number_of_zones", "cooktop")
        self.assertEqual(loss_class, "validation_unresolved")
        self.assertFalse(chain["source_candidate_exists"])
        self.assertTrue(chain["targeted_fetched_ok"])
        self.assertTrue(chain["targeted_extracted"])
        self.assertTrue(chain["targeted_mapped"])
        self.assertTrue(chain["in_final_profile"])

    def test_fact_for_another_field_does_not_set_targeted_mapping(self):
        qr = _query_result(
            fetched=[_fetch(status="success")],
            extracted=[_raw("power")],
            mapped=[_canonical_attr("power")],
        )
        result = _make_result(
            targeted_fields=[_targeted_field("number_of_zones", qr=[qr])],
        )
        loss_class, _, chain = classify_loss(result, "number_of_zones", "cooktop")
        self.assertEqual(loss_class, "extraction_missed")
        self.assertFalse(chain["targeted_extracted"])
        self.assertFalse(chain["targeted_mapped"])

    def test_targeted_identity_rejections_are_not_called_not_selected(self):
        candidates = [_candidate("https://example.com/a")]
        qr = _query_result(candidates=candidates, rejected=[SimpleNamespace(candidate=candidates[0])])
        result = _make_result(
            targeted_fields=[_targeted_field("number_of_zones", qr=[qr])],
        )
        loss_class, _, _ = classify_loss(result, "number_of_zones", "cooktop")
        self.assertEqual(loss_class, "source_rejected")

    def test_targeted_candidate_not_fetched_is_not_selected(self):
        qr = _query_result(candidates=[_candidate("https://example.com/a")])
        result = _make_result(
            targeted_fields=[_targeted_field("number_of_zones", qr=[qr])],
        )
        loss_class, _, _ = classify_loss(result, "number_of_zones", "cooktop")
        self.assertEqual(loss_class, "source_not_selected")


# ---------------------------------------------------------------------------
# Tests – chain structure
# ---------------------------------------------------------------------------

class TestChainHasAllKeys(unittest.TestCase):
    def test_chain_contains_all_11_keys(self):
        result = _make_result()
        _, _, chain = classify_loss(result, "number_of_zones", "cooktop")
        self.assertEqual(set(chain.keys()), set(CHAIN_KEYS))

    def test_chain_has_exactly_11_keys(self):
        result = _make_result(
            candidates=[_candidate()],
            selected=[_candidate()],
            fetched=[_fetch(status="success")],
        )
        _, _, chain = classify_loss(result, "number_of_zones", "cooktop")
        self.assertEqual(len(chain), 11)


# ---------------------------------------------------------------------------
# Tests – diagnose_result
# ---------------------------------------------------------------------------

class TestDiagnoseResultReturnsDict(unittest.TestCase):
    def _make_full_result(self):
        return _make_result(
            candidates=[_candidate()],
            selected=[_candidate()],
            fetched=[_fetch(status="success")],
            raw_attrs=[],
            category_id="cooktop",
        )

    def test_returns_dict_with_required_keys(self):
        result = self._make_full_result()
        report = diagnose_result(AUDIT_PRODUCT, result)
        for key in (
            "product", "expected_category", "detected_category",
            "schema_total", "schema_found", "schema_missing", "coverage_percent",
            "raw_attribute_count", "canonical_mapped_count", "successful_fetches",
            "top_sources", "missing_attributes", "loss_class_breakdown", "bottleneck",
        ):
            self.assertIn(key, report, f"missing key: {key}")

    def test_schema_found_plus_missing_equals_total(self):
        result = self._make_full_result()
        report = diagnose_result(AUDIT_PRODUCT, result)
        self.assertEqual(
            report["schema_found"] + report["schema_missing"],
            report["schema_total"],
        )


class TestLossBreakdownSumsToMissing(unittest.TestCase):
    def test_sum_of_breakdown_equals_schema_missing_count(self):
        result = _make_result(
            candidates=[_candidate()],
            selected=[_candidate()],
            fetched=[_fetch(status="success")],
            raw_attrs=[],
            category_id="cooktop",
        )
        report = diagnose_result(AUDIT_PRODUCT, result)
        total = sum(report["loss_class_breakdown"].values())
        self.assertEqual(total, report["schema_missing"])

    def test_unresolved_found_attributes_are_reported_separately(self):
        result = _make_result(
            candidates=[_candidate()],
            selected=[_candidate()],
            fetched=[_fetch(status="success")],
            raw_attrs=[_raw("number of zones")],
            mapped=[_canonical_attr("number_of_zones")],
            profile_attrs=[_profile_attr("number_of_zones", status="Unresolved")],
            category_id="cooktop",
        )
        report = diagnose_result(AUDIT_PRODUCT, result)
        self.assertIn("number_of_zones", report["schema_found_list"])
        self.assertNotIn("number_of_zones", report["missing_attributes"])
        self.assertEqual(
            report["unresolved_attributes"]["number_of_zones"]["loss_class"],
            "validation_unresolved",
        )
        self.assertEqual(
            sum(report["missing_loss_class_breakdown"].values()),
            report["schema_missing"],
        )
        self.assertEqual(
            sum(report["loss_class_breakdown"].values()),
            report["schema_missing"] + 1,
        )


class TestBottleneckIsDominantClass(unittest.TestCase):
    def test_bottleneck_is_class_with_highest_count(self):
        # All fetches succeed, no raw attrs → everything should be extraction_missed
        result = _make_result(
            candidates=[_candidate()],
            selected=[_candidate()],
            fetched=[_fetch(status="success")],
            raw_attrs=[],
            category_id="cooktop",
        )
        report = diagnose_result(AUDIT_PRODUCT, result)
        breakdown = report["loss_class_breakdown"]
        max_count = max(breakdown.values())
        self.assertEqual(breakdown[report["bottleneck"]], max_count)

    def test_bottleneck_none_when_no_missing(self):
        # All schema attributes mapped
        from core.schema import get_attribute_schema
        schema = get_attribute_schema("cooktop")
        mapped = [_canonical_attr(d.canonical_name) for d in schema]
        result = _make_result(
            candidates=[_candidate()],
            selected=[_candidate()],
            fetched=[_fetch(status="success")],
            raw_attrs=[],
            mapped=mapped,
            category_id="cooktop",
        )
        report = diagnose_result(AUDIT_PRODUCT, result)
        self.assertEqual(report["schema_missing"], 0)
        self.assertIsNone(report["bottleneck"])


class TestNoPipelineMutation(unittest.TestCase):
    def test_diagnose_result_does_not_mutate_result(self):
        """Calling diagnose_result must not change any field on the result object."""
        result = _make_result(
            candidates=[_candidate()],
            selected=[_candidate()],
            fetched=[_fetch(status="success")],
            raw_attrs=[],
            category_id="cooktop",
        )
        original_candidates = list(result.discovery.candidates)
        original_selected = tuple(result.selected_candidates)
        original_fetched = tuple(result.fetched_sources)
        original_raw = tuple(result.raw_attributes)

        diagnose_result(AUDIT_PRODUCT, result)

        self.assertEqual(result.discovery.candidates, original_candidates)
        self.assertEqual(result.selected_candidates, original_selected)
        self.assertEqual(result.fetched_sources, original_fetched)
        self.assertEqual(result.raw_attributes, original_raw)


class TestDiagnoseResultCategoryMismatch(unittest.TestCase):
    def test_detected_category_preserved_in_report(self):
        """detected_category reflects what the pipeline actually returned."""
        result = _make_result(
            candidates=[_candidate()],
            selected=[_candidate()],
            fetched=[_fetch(status="success")],
            raw_attrs=[],
            category_id="air_fryer",  # different from AUDIT_PRODUCT.category
        )
        report = diagnose_result(AUDIT_PRODUCT, result)
        # expected_category comes from the AuditProduct, not the detection
        self.assertEqual(report["expected_category"], "cooktop")
        # detected_category reflects what the pipeline returned
        self.assertEqual(report["detected_category"], "air_fryer")


if __name__ == "__main__":
    unittest.main()
