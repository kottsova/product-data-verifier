"""Deterministic Stage 10 application service boundary tests (no live network)."""

from __future__ import annotations

from dataclasses import fields, is_dataclass, replace
import json
import unittest
from types import SimpleNamespace

from core.identity import ProductIdentity
from core.profile import EvidenceRecord, FinalProductProfile, ProfileAttribute
from core.quality import QualityAssessment, assess_product_quality
from core.validation import ValidatedFact, ValidatedProductProfile
from core.workflow import ProductWorkflowResult
from services.product_verifier import (
    ProductVerifierService,
    ServiceAttribute,
    ServiceCategory,
    ServiceIdentity,
    ServiceQuality,
    VerifyProductError,
    VerifyProductRequest,
    VerifyProductResult,
    verify_product,
)
from tests.test_profile_export import candidate, definition, final_profile


# Every internal pipeline DTO type that must never be reachable from a
# VerifyProductResult, directly or nested inside it.
_INTERNAL_TYPES = (
    ProductWorkflowResult, FinalProductProfile, ProfileAttribute, EvidenceRecord,
    ProductIdentity, QualityAssessment, ValidatedFact, ValidatedProductProfile,
)


def _walk(value, seen=None):
    """Yield every value reachable from `value` through dataclass fields,
    tuples/lists/sets, and dict keys/values -- for a recursive leak check."""
    seen = seen if seen is not None else set()
    if id(value) in seen:
        return
    seen.add(id(value))
    yield value
    if is_dataclass(value) and not isinstance(value, type):
        for item in fields(value):
            yield from _walk(getattr(value, item.name), seen)
    elif isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            yield from _walk(item, seen)
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _walk(key, seen)
            yield from _walk(item, seen)


def fake_runner(profile, *, quality=None):
    """A deterministic stand-in for core.workflow.run_product_workflow."""
    def _run(_internal_request):
        if quality is not None:
            return SimpleNamespace(final_profile=profile, quality=quality)
        return SimpleNamespace(final_profile=profile)
    return _run


def raising_runner(exception):
    def _run(_internal_request):
        raise exception
    return _run


class ValidRequestMappingTests(unittest.TestCase):
    def setUp(self):
        self.profile = final_profile(
            [definition("power")],
            [candidate("power", "1000", unit="W")],
        )

    def test_valid_request_produces_a_stable_successful_result(self):
        service = ProductVerifierService(run_workflow=fake_runner(self.profile))
        result = service.verify(VerifyProductRequest(brand="Acme", model="X100"))
        self.assertIsInstance(result, VerifyProductResult)
        self.assertTrue(result.success)
        self.assertIsNone(result.error)

    def test_workflow_success_maps_identity_category_and_attributes(self):
        service = ProductVerifierService(run_workflow=fake_runner(self.profile))
        result = service.verify(VerifyProductRequest(brand="Acme", model="X100"))

        self.assertIsInstance(result.identity, ServiceIdentity)
        self.assertEqual(result.identity.brand, "Acme")
        self.assertEqual(result.identity.base_model, "X100")

        self.assertIsInstance(result.category, ServiceCategory)
        self.assertEqual(result.category.category_id, "cooktop")

        self.assertEqual(len(result.attributes), 1)
        attribute = result.attributes[0]
        self.assertIsInstance(attribute, ServiceAttribute)
        self.assertEqual(attribute.canonical_name, "power")
        self.assertEqual(attribute.status, "Confirmed")
        self.assertEqual(attribute.value, "1000")

    def test_quality_is_taken_from_the_workflow_result_when_present(self):
        precomputed = assess_product_quality(self.profile)
        # A distinguishable sentinel so we can prove it was reused, not recomputed.
        sentinel = replace(precomputed, warnings=("sentinel-marker",))
        service = ProductVerifierService(run_workflow=fake_runner(self.profile, quality=sentinel))
        result = service.verify(VerifyProductRequest(brand="Acme", model="X100"))
        self.assertEqual(result.quality.warnings, ("sentinel-marker",))

    def test_quality_is_computed_when_the_workflow_result_lacks_it(self):
        service = ProductVerifierService(run_workflow=fake_runner(self.profile))
        result = service.verify(VerifyProductRequest(brand="Acme", model="X100"))
        self.assertEqual(result.quality.to_dict(), assess_product_quality(self.profile).to_dict())

    def test_quality_is_a_service_dto_not_the_internal_quality_assessment(self):
        service = ProductVerifierService(run_workflow=fake_runner(self.profile))
        result = service.verify(VerifyProductRequest(brand="Acme", model="X100"))
        self.assertIsInstance(result.quality, ServiceQuality)
        self.assertNotIsInstance(result.quality, QualityAssessment)

    def test_full_quality_data_is_preserved_field_by_field(self):
        assessment = QualityAssessment(
            status="partial",
            coverage_percent=42.5,
            schema_total=10,
            schema_found=6,
            confirmed_count=4,
            unresolved_count=5,
            conflict_count=1,
            critical_total=3,
            critical_found=2,
            critical_confirmed=1,
            critical_conflict=0,
            critical_high_authority_confirmed=1,
            category_confidence="medium",
            identity_confidence="high",
            reasons=("reason one", "reason two"),
            warnings=("warning one",),
        )
        service = ProductVerifierService(run_workflow=fake_runner(self.profile, quality=assessment))
        result = service.verify(VerifyProductRequest(brand="Acme", model="X100"))
        self.assertIsInstance(result.quality, ServiceQuality)
        self.assertNotIsInstance(result.quality, QualityAssessment)
        self.assertEqual(result.quality.to_dict(), assessment.to_dict())

    def test_provenance_and_evidence_are_preserved(self):
        profile = final_profile(
            [definition("power")],
            [
                candidate("power", "1000", unit="W", source="https://one.example/X100",
                          evidence="power: 1000 W (spec table)"),
            ],
        )
        service = ProductVerifierService(run_workflow=fake_runner(profile))
        result = service.verify(VerifyProductRequest(brand="Acme", model="X100"))
        attribute = result.attributes[0]
        self.assertEqual(len(attribute.supporting_sources), 1)
        evidence = attribute.supporting_sources[0]
        self.assertEqual(evidence.source, "https://one.example/X100")
        self.assertEqual(evidence.evidence, "power: 1000 W (spec table)")
        self.assertEqual(evidence.authority_status, "verified")

    def test_unresolved_and_conflicts_are_named(self):
        profile = final_profile(
            [definition("power"), definition("gross_weight", value_type="weight")],
            [candidate("power", "1000", unit="W")],
        )
        service = ProductVerifierService(run_workflow=fake_runner(profile))
        result = service.verify(VerifyProductRequest(brand="Acme", model="X100"))
        self.assertIn("gross_weight", result.unresolved)


class ErrorContractTests(unittest.TestCase):
    def test_invalid_brand_produces_invalid_request_error(self):
        service = ProductVerifierService(run_workflow=fake_runner(None))
        result = service.verify(VerifyProductRequest(brand="", model="X100"))
        self.assertFalse(result.success)
        self.assertIsInstance(result.error, VerifyProductError)
        self.assertEqual(result.error.kind, "invalid_request")

    def test_invalid_model_produces_invalid_request_error(self):
        service = ProductVerifierService(run_workflow=fake_runner(None))
        result = service.verify(VerifyProductRequest(brand="Acme", model="   "))
        self.assertFalse(result.success)
        self.assertEqual(result.error.kind, "invalid_request")

    def test_invalid_request_never_calls_the_workflow(self):
        calls = []

        def tracking_runner(_request):
            calls.append(_request)
            raise AssertionError("workflow must not run for an invalid request")

        service = ProductVerifierService(run_workflow=tracking_runner)
        result = service.verify(VerifyProductRequest(brand="", model=""))
        self.assertFalse(result.success)
        self.assertEqual(calls, [])

    def test_runtime_error_from_workflow_becomes_workflow_failure(self):
        service = ProductVerifierService(run_workflow=raising_runner(RuntimeError("network down")))
        result = service.verify(VerifyProductRequest(brand="Acme", model="X100"))
        self.assertFalse(result.success)
        self.assertEqual(result.error.kind, "workflow_failure")
        self.assertEqual(result.error.message, "network down")
        self.assertEqual(result.error.detail, "RuntimeError")

    def test_value_error_from_workflow_becomes_workflow_failure(self):
        service = ProductVerifierService(run_workflow=raising_runner(ValueError("bad state")))
        result = service.verify(VerifyProductRequest(brand="Acme", model="X100"))
        self.assertFalse(result.success)
        self.assertEqual(result.error.kind, "workflow_failure")

    def test_unexpected_exception_type_becomes_internal_error_without_leaking_it(self):
        service = ProductVerifierService(run_workflow=raising_runner(KeyError("boom")))
        result = service.verify(VerifyProductRequest(brand="Acme", model="X100"))
        self.assertFalse(result.success)
        self.assertEqual(result.error.kind, "internal_error")
        self.assertEqual(result.error.detail, "KeyError")
        # The public error is a plain, JSON-safe dataclass -- never the raw exception.
        self.assertIsInstance(result.error.message, str)
        json.dumps(result.error.to_dict())  # must not raise

    def test_invalid_error_kind_is_rejected(self):
        with self.assertRaises(ValueError):
            VerifyProductError(kind="bogus", message="x")  # type: ignore[arg-type]


class NoInternalLeakageTests(unittest.TestCase):
    def setUp(self):
        self.profile = final_profile(
            [definition("power")],
            [candidate("power", "1000", unit="W")],
        )

    def test_public_result_holds_only_service_dataclasses(self):
        service = ProductVerifierService(run_workflow=fake_runner(self.profile))
        result = service.verify(VerifyProductRequest(brand="Acme", model="X100"))
        self.assertNotIsInstance(result.identity, ProductIdentity)
        self.assertIsInstance(result.identity, ServiceIdentity)
        for attribute in result.attributes:
            self.assertNotIsInstance(attribute, ProfileAttribute)
            self.assertIsInstance(attribute, ServiceAttribute)
        self.assertFalse(hasattr(result, "final_profile"))

    def test_verify_does_not_return_the_raw_workflow_result(self):
        service = ProductVerifierService(run_workflow=fake_runner(self.profile))
        result = service.verify(VerifyProductRequest(brand="Acme", model="X100"))
        self.assertNotIsInstance(result, ProductWorkflowResult)
        self.assertNotIsInstance(result, FinalProductProfile)

    def test_no_internal_dto_is_reachable_anywhere_in_the_result_graph(self):
        """Recursively walk the whole VerifyProductResult and check every node."""
        precomputed = assess_product_quality(self.profile)
        service = ProductVerifierService(run_workflow=fake_runner(self.profile, quality=precomputed))
        result = service.verify(VerifyProductRequest(brand="Acme", model="X100"))
        offenders = [
            node for node in _walk(result)
            if isinstance(node, _INTERNAL_TYPES)
        ]
        self.assertEqual(offenders, [])

    def test_no_internal_dto_is_reachable_in_a_failure_result(self):
        service = ProductVerifierService(run_workflow=raising_runner(RuntimeError("network down")))
        result = service.verify(VerifyProductRequest(brand="Acme", model="X100"))
        offenders = [node for node in _walk(result) if isinstance(node, _INTERNAL_TYPES)]
        self.assertEqual(offenders, [])


class SerializationStabilityTests(unittest.TestCase):
    def test_successful_result_serializes_deterministically_and_is_json_safe(self):
        profile = final_profile([definition("power")], [candidate("power", "1000", unit="W")])
        service = ProductVerifierService(run_workflow=fake_runner(profile))
        request = VerifyProductRequest(brand="Acme", model="X100")
        first = service.verify(request)
        second = service.verify(request)
        self.assertEqual(first.to_dict(), second.to_dict())
        round_tripped = json.loads(json.dumps(first.to_dict(), ensure_ascii=False))
        self.assertEqual(round_tripped, first.to_dict())
        self.assertEqual(round_tripped["attributes"][0]["status"], "Confirmed")
        self.assertIn(round_tripped["quality"]["status"], (
            "verified", "partial", "insufficient", "conflicted",
        ))
        self.assertEqual(round_tripped["quality"], first.quality.to_dict())

    def test_failure_result_serializes_deterministically_and_is_json_safe(self):
        service = ProductVerifierService(run_workflow=fake_runner(None))
        result = service.verify(VerifyProductRequest(brand="", model="X100"))
        data = result.to_dict()
        self.assertFalse(data["success"])
        self.assertEqual(data["error"]["kind"], "invalid_request")
        json.dumps(data)  # must not raise


class DependencyInjectionTests(unittest.TestCase):
    def test_module_level_helper_accepts_an_injected_service(self):
        profile = final_profile([definition("power")], [candidate("power", "1000", unit="W")])
        injected = ProductVerifierService(run_workflow=fake_runner(profile))
        result = verify_product(VerifyProductRequest(brand="Acme", model="X100"), service=injected)
        self.assertTrue(result.success)

    def test_no_global_mutable_singleton_is_shared_between_services(self):
        profile_a = final_profile([definition("power")], [candidate("power", "1000", unit="W")])
        profile_b = final_profile([definition("power")], [candidate("power", "2000", unit="W")])
        service_a = ProductVerifierService(run_workflow=fake_runner(profile_a))
        service_b = ProductVerifierService(run_workflow=fake_runner(profile_b))
        request = VerifyProductRequest(brand="Acme", model="X100")
        result_a = service_a.verify(request)
        result_b = service_b.verify(request)
        self.assertEqual(result_a.attributes[0].value, "1000")
        self.assertEqual(result_b.attributes[0].value, "2000")


if __name__ == "__main__":
    unittest.main()
