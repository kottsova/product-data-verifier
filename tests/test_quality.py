"""Deterministic Stage 9 quality gate tests (no live network access)."""

from __future__ import annotations

import json
import unittest
from dataclasses import replace

from core.category import CATEGORY_NAMES, CategoryResult
from core.export import profile_to_dict
from core.identity import ProductIdentity
from core.profile import build_final_profile
from core.quality import (
    DEFAULT_THRESHOLDS,
    QualityThresholds,
    assess_product_quality,
)
from core.schema import get_attribute_schema
from core.validation import validate_product_profile
from tests.test_profile_export import IDENTITY, candidate, definition


def category_result(category_id="cooktop", confidence="high"):
    name, parent = CATEGORY_NAMES[category_id]
    return CategoryResult(
        category_id=category_id,
        category_name=name,
        parent_category=parent,
        confidence=confidence,
        evidence=["synthetic category evidence"],
        source="mixed",
    )


def profile_with(
    schema,
    facts=(),
    *,
    category_id="cooktop",
    category_confidence="high",
    identity=IDENTITY,
):
    result = category_result(category_id, category_confidence)
    validated = validate_product_profile(identity, result, list(facts), schema=schema)
    return build_final_profile(validated, category_result=result, schema=schema)


# A compact synthetic schema: two universal-style critical identity fields
# plus two category-critical fields and two non-critical expected fields.
FULL_SCHEMA = [
    definition("brand", priority="critical"),
    definition("model", priority="critical"),
    definition("critical_a", priority="critical"),
    definition("critical_b", priority="critical"),
    definition("high_a", priority="high"),
    definition("high_b", priority="high"),
]


def strong(name, value="Confirmed value", source="https://official.example/a"):
    return candidate(name, value, source=source, authority="verified", source_type="manufacturer")


def weak(name, value="Weak value", source="https://reseller.example/a"):
    return candidate(name, value, source=source, authority="unverified", source_type="retailer")


def specialized(name, value="Reference value", source="https://spec-reference.example/a"):
    """A Confirmed-capable fact from a specialized reference, not a manufacturer.

    Reaches authority rank 3 (not verified rank 4), so validate.py resolves it
    as Confirmed with confidence="medium" rather than "high" -- the generic,
    already-computed source-authority signal this policy reads.
    """
    return candidate(name, value, source=source, authority="unknown", source_type="specialized_reference")


class QualityPolicyTests(unittest.TestCase):
    def test_high_coverage_and_confirmed_criticals_is_verified(self):
        facts = [strong(name) for name in (
            "brand", "model", "critical_a", "critical_b", "high_a", "high_b",
        )]
        profile = profile_with(FULL_SCHEMA, facts)
        assessment = assess_product_quality(profile)
        self.assertEqual(assessment.status, "verified")
        self.assertEqual(assessment.coverage_percent, 100.0)
        self.assertEqual(assessment.critical_confirmed, 4)
        self.assertEqual(assessment.critical_total, 4)
        self.assertTrue(assessment.reasons)

    def test_medium_coverage_with_unresolved_is_partial(self):
        facts = [
            strong("brand"), strong("model"),
            weak("critical_a"),  # found but not confirmed -> confirmed ratio drops below 0.75
            weak("critical_b"),
        ]
        profile = profile_with(FULL_SCHEMA, facts)
        assessment = assess_product_quality(profile)
        self.assertEqual(assessment.status, "partial")
        self.assertEqual(assessment.critical_confirmed, 2)
        self.assertEqual(assessment.critical_found, 4)
        self.assertEqual(assessment.unresolved_count, 4)  # critical_a, critical_b, high_a, high_b

    def test_low_coverage_and_missing_criticals_is_insufficient(self):
        schema = [
            definition("brand", priority="critical"),
            definition("model", priority="critical"),
            definition("critical_a", priority="critical"),
            definition("critical_b", priority="critical"),
            definition("critical_c", priority="critical"),
            definition("critical_d", priority="critical"),
            definition("high_a", priority="high"),
            definition("high_b", priority="high"),
            definition("high_c", priority="high"),
        ]
        facts = [strong("brand"), strong("model")]
        profile = profile_with(schema, facts)
        assessment = assess_product_quality(profile)
        self.assertEqual(assessment.status, "insufficient")
        self.assertLess(assessment.coverage_percent, 25.0)
        self.assertLess(assessment.critical_found / assessment.critical_total, 0.34)

    def test_critical_conflict_forces_conflicted(self):
        facts = [
            strong("brand"), strong("model"),
            strong("critical_a", "Value A", source="https://official.example/a"),
            strong("critical_a", "Value B", source="https://official.example/b"),
            strong("critical_b"),
            strong("high_a"), strong("high_b"),
        ]
        profile = profile_with(FULL_SCHEMA, facts)
        assessment = assess_product_quality(profile)
        self.assertEqual(assessment.status, "conflicted")
        self.assertEqual(assessment.critical_conflict, 1)
        self.assertIn("critical_a", assessment.reasons[0])

    def test_non_critical_conflict_does_not_force_conflicted(self):
        facts = [
            strong("brand"), strong("model"),
            strong("critical_a"), strong("critical_b"),
            strong("high_a", "Value A", source="https://official.example/a"),
            strong("high_a", "Value B", source="https://official.example/b"),
            strong("high_b"),
        ]
        profile = profile_with(FULL_SCHEMA, facts)
        assessment = assess_product_quality(profile)
        self.assertNotEqual(assessment.status, "conflicted")
        self.assertEqual(assessment.status, "verified")
        self.assertEqual(assessment.critical_conflict, 0)
        self.assertTrue(any("non-critical" in warning for warning in assessment.warnings))

    def test_source_authority_alone_can_prevent_verified_status(self):
        """Full critical coverage/confirmation via weak authority stays below verified.

        This isolates the "source authority / evidence quality" signal: every
        critical field is Confirmed (so critical_found/confirmed ratios are
        both 1.0) and overall coverage is 100%, yet only one of four
        confirmations came from a manufacturer-verified source. The others
        came from a specialized reference (confidence="medium"), which the
        default policy does not accept as sufficient for "verified".
        """
        facts = [
            strong("brand"),  # manufacturer-verified: confidence="high"
            specialized("model"),
            specialized("critical_a"),
            specialized("critical_b"),
            strong("high_a"), strong("high_b"),
        ]
        profile = profile_with(FULL_SCHEMA, facts)
        assessment = assess_product_quality(profile)
        self.assertEqual(assessment.coverage_percent, 100.0)
        self.assertEqual(assessment.critical_confirmed, 4)
        self.assertEqual(assessment.critical_high_authority_confirmed, 1)
        self.assertEqual(assessment.status, "partial")
        self.assertTrue(any("specialized-reference" in warning for warning in assessment.warnings))

    def test_lowering_authority_threshold_allows_verified_on_the_same_profile(self):
        """The authority ratio is a centralized, testable threshold, not a fact change."""
        facts = [
            strong("brand"),
            specialized("model"),
            specialized("critical_a"),
            specialized("critical_b"),
            strong("high_a"), strong("high_b"),
        ]
        profile = profile_with(FULL_SCHEMA, facts)
        lenient = QualityThresholds(verified_min_high_authority_ratio=0.0)
        assessment = assess_product_quality(profile, thresholds=lenient)
        self.assertEqual(assessment.status, "verified")
        # The underlying facts/counts are identical; only the threshold moved.
        self.assertEqual(assessment.critical_high_authority_confirmed, 1)

    def test_low_category_confidence_prevents_verified_despite_full_critical_coverage(self):
        facts = [strong(name) for name in (
            "brand", "model", "critical_a", "critical_b", "high_a", "high_b",
        )]
        profile = profile_with(FULL_SCHEMA, facts, category_confidence="low")
        assessment = assess_product_quality(profile)
        self.assertEqual(assessment.category_confidence, "low")
        self.assertNotEqual(assessment.status, "insufficient")  # category is known, just weakly
        self.assertEqual(assessment.status, "partial")
        self.assertTrue(any("Category confidence is low" in warning for warning in assessment.warnings))

    def test_low_identity_confidence_forces_insufficient_even_with_confirmed_identity_fields(self):
        facts = [strong(name) for name in (
            "brand", "model", "critical_a", "critical_b", "high_a", "high_b",
        )]
        weak_identity = replace(IDENTITY, confidence="low")
        profile = profile_with(FULL_SCHEMA, facts, identity=weak_identity)
        assessment = assess_product_quality(profile)
        self.assertEqual(assessment.identity_confidence, "low")
        self.assertEqual(assessment.status, "insufficient")
        self.assertIn("low confidence", assessment.reasons[0])

    def test_unknown_category_is_insufficient(self):
        facts = [strong(name) for name in (
            "brand", "model", "critical_a", "critical_b", "high_a", "high_b",
        )]
        profile = profile_with(
            FULL_SCHEMA, facts, category_id="unknown", category_confidence="low",
        )
        assessment = assess_product_quality(profile)
        self.assertEqual(assessment.status, "insufficient")
        self.assertIn("Category could not be determined", assessment.reasons[0])

    def test_missing_model_is_insufficient(self):
        facts = [
            strong("brand"),
            strong("critical_a"), strong("critical_b"),
            strong("high_a"), strong("high_b"),
        ]
        profile = profile_with(FULL_SCHEMA, facts)
        assessment = assess_product_quality(profile)
        self.assertEqual(assessment.status, "insufficient")
        self.assertIn("model", assessment.reasons[0])

    def test_missing_brand_is_insufficient(self):
        facts = [
            strong("model"),
            strong("critical_a"), strong("critical_b"),
            strong("high_a"), strong("high_b"),
        ]
        profile = profile_with(FULL_SCHEMA, facts)
        assessment = assess_product_quality(profile)
        self.assertEqual(assessment.status, "insufficient")
        self.assertIn("brand", assessment.reasons[0])

    def test_thresholds_are_centralized_and_respected_at_boundaries(self):
        schema = [
            definition("brand", priority="critical"),
            definition("model", priority="critical"),
            definition("high_a", priority="high"),
            definition("high_b", priority="high"),
        ]
        thresholds = QualityThresholds(
            insufficient_max_coverage=0.2,
            insufficient_max_critical_found_ratio=0.2,
            verified_min_coverage=0.75,
            verified_min_critical_confirmed_ratio=1.0,
            verified_min_critical_found_ratio=1.0,
        )
        base_facts = [strong("brand"), strong("model")]

        at_boundary = profile_with(schema, [*base_facts, strong("high_a")])
        at_boundary_assessment = assess_product_quality(at_boundary, thresholds=thresholds)
        self.assertEqual(at_boundary_assessment.coverage_percent, 75.0)
        self.assertEqual(at_boundary_assessment.status, "verified")

        below_boundary = profile_with(schema, base_facts)
        below_assessment = assess_product_quality(below_boundary, thresholds=thresholds)
        self.assertEqual(below_assessment.coverage_percent, 50.0)
        self.assertEqual(below_assessment.status, "partial")

    def test_default_thresholds_are_a_frozen_reusable_singleton(self):
        self.assertIsInstance(DEFAULT_THRESHOLDS, QualityThresholds)
        with self.assertRaises(Exception):
            DEFAULT_THRESHOLDS.verified_min_coverage = 0.9  # type: ignore[misc]

    def test_invalid_threshold_ratio_is_rejected(self):
        with self.assertRaises(ValueError):
            QualityThresholds(verified_min_coverage=1.5)

    def test_assessment_does_not_mutate_final_profile(self):
        facts = [strong(name) for name in ("brand", "model", "critical_a", "critical_b")]
        profile = profile_with(FULL_SCHEMA, facts)
        before = profile_to_dict(profile)
        assess_product_quality(profile)
        after = profile_to_dict(profile)
        self.assertEqual(before, after)

    def test_serialization_is_stable_and_json_safe(self):
        facts = [strong(name) for name in (
            "brand", "model", "critical_a", "critical_b", "high_a", "high_b",
        )]
        profile = profile_with(FULL_SCHEMA, facts)
        first = assess_product_quality(profile)
        second = assess_product_quality(profile)
        self.assertEqual(first, second)
        self.assertEqual(first.to_dict(), second.to_dict())
        # Round-trips without raising and without silently dropping fields.
        round_tripped = json.loads(json.dumps(first.to_dict(), ensure_ascii=False))
        self.assertEqual(round_tripped, first.to_dict())

    def test_invalid_status_is_rejected(self):
        assessment_kwargs = dict(
            coverage_percent=0.0, schema_total=0, schema_found=0, confirmed_count=0,
            unresolved_count=0, conflict_count=0, critical_total=0, critical_found=0,
            critical_confirmed=0, critical_conflict=0, critical_high_authority_confirmed=0,
            category_confidence="low",
            identity_confidence="low", reasons=(), warnings=(),
        )
        from core.quality import QualityAssessment
        with self.assertRaises(ValueError):
            QualityAssessment(status="bogus", **assessment_kwargs)  # type: ignore[arg-type]


class QualityCategoryPolicyTests(unittest.TestCase):
    """Each production category's critical fields come from its own schema."""

    def _confirm_all_expected(self, category_id, identity=IDENTITY):
        schema = get_attribute_schema(category_id)
        facts = [
            strong(item.canonical_name, f"Value for {item.canonical_name}")
            for item in schema if item.expected
        ]
        return schema, facts

    def test_each_category_critical_set_matches_its_own_schema(self):
        for category_id in ("cooktop", "smartphone", "sewing_machine", "air_fryer", "wet_dry_vacuum"):
            with self.subTest(category=category_id):
                schema, facts = self._confirm_all_expected(category_id)
                profile = profile_with(schema, facts, category_id=category_id)
                assessment = assess_product_quality(profile)
                expected_critical_total = sum(
                    1 for item in schema if item.expected and item.priority == "critical"
                )
                self.assertEqual(assessment.critical_total, expected_critical_total)
                self.assertGreater(assessment.critical_total, 0)
                self.assertEqual(assessment.status, "verified")
                self.assertEqual(assessment.critical_confirmed, expected_critical_total)

    def test_each_category_with_no_evidence_is_insufficient(self):
        for category_id in ("cooktop", "smartphone", "sewing_machine", "air_fryer", "wet_dry_vacuum"):
            with self.subTest(category=category_id):
                schema = get_attribute_schema(category_id)
                profile = profile_with(schema, (), category_id=category_id)
                assessment = assess_product_quality(profile)
                self.assertEqual(assessment.status, "insufficient")


if __name__ == "__main__":
    unittest.main()
