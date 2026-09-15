"""Deterministic Stage 11 cache/persistence tests (no live network)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from services.cache import (
    CacheEntry,
    CachePolicy,
    InMemoryProductVerificationRepository,
    SqliteProductVerificationRepository,
)
from services.product_verifier import (
    ProductVerifierService,
    VerifyProductRequest,
    _cache_key,
)
from tests.test_product_verifier import _INTERNAL_TYPES, _walk, fake_runner, raising_runner
from tests.test_profile_export import candidate, definition, final_profile


class FakeClock:
    """A deterministic, manually-advanced clock for TTL tests."""

    def __init__(self, start: float = 1_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class CountingRunner:
    """Wraps fake_runner but records how many times the workflow actually ran."""

    def __init__(self, profile) -> None:
        self.calls = 0
        self._inner = fake_runner(profile)

    def __call__(self, request):
        self.calls += 1
        return self._inner(request)


class RaisingRepository:
    """A repository whose calls fail, to test graceful degradation."""

    def __init__(self, *, fail_get: bool = False, fail_save: bool = False) -> None:
        self.fail_get = fail_get
        self.fail_save = fail_save
        self.save_calls = 0

    def get(self, key):
        if self.fail_get:
            raise RuntimeError("storage unavailable")
        return None

    def save(self, entry):
        self.save_calls += 1
        if self.fail_save:
            raise RuntimeError("storage unavailable")

    def delete(self, key):
        pass

    def clear(self):
        pass


def request(**overrides):
    defaults = dict(brand="Acme", model="X100")
    defaults.update(overrides)
    return VerifyProductRequest(**defaults)


def sample_profile(value="1000"):
    return final_profile(
        [definition("power")],
        [candidate(
            "power", value, unit="W", source="https://one.example/X100",
            evidence="power reading",
        )],
    )


class CacheMissAndHitTests(unittest.TestCase):
    def test_cache_miss_calls_workflow_and_saves_result(self):
        repository = InMemoryProductVerificationRepository()
        runner = CountingRunner(sample_profile())
        service = ProductVerifierService(run_workflow=runner, repository=repository)
        result = service.verify(request())
        self.assertTrue(result.success)
        self.assertFalse(result.served_from_cache)
        self.assertEqual(runner.calls, 1)
        self.assertIsNotNone(repository.get(next(iter(repository._entries))))

    def test_fresh_cache_hit_does_not_call_workflow(self):
        repository = InMemoryProductVerificationRepository()
        runner = CountingRunner(sample_profile())
        service = ProductVerifierService(run_workflow=runner, repository=repository)
        req = request()
        first = service.verify(req)
        second = service.verify(req)
        self.assertEqual(runner.calls, 1)
        self.assertFalse(first.served_from_cache)
        self.assertTrue(second.served_from_cache)
        self.assertEqual(second.attributes[0].value, first.attributes[0].value)

    def test_stale_entry_triggers_workflow_again(self):
        clock = FakeClock()
        repository = InMemoryProductVerificationRepository()
        runner = CountingRunner(sample_profile())
        policy = CachePolicy(ttl_seconds=60)
        service = ProductVerifierService(
            run_workflow=runner, repository=repository, cache_policy=policy, clock=clock,
        )
        req = request()
        service.verify(req)
        clock.advance(61)
        result = service.verify(req)
        self.assertEqual(runner.calls, 2)
        self.assertFalse(result.served_from_cache)

    def test_cache_age_and_stored_at_are_reported_on_a_hit(self):
        clock = FakeClock()
        repository = InMemoryProductVerificationRepository()
        service = ProductVerifierService(
            run_workflow=CountingRunner(sample_profile()), repository=repository, clock=clock,
        )
        req = request()
        service.verify(req)
        clock.advance(15)
        result = service.verify(req)
        self.assertTrue(result.served_from_cache)
        self.assertEqual(result.cache_age_seconds, 15)
        self.assertEqual(result.cache_stored_at, 1_000_000.0)

    def test_configurable_ttl_boundary(self):
        clock = FakeClock()
        repository = InMemoryProductVerificationRepository()
        runner = CountingRunner(sample_profile())
        policy = CachePolicy(ttl_seconds=10)
        service = ProductVerifierService(
            run_workflow=runner, repository=repository, cache_policy=policy, clock=clock,
        )
        req = request()
        service.verify(req)
        clock.advance(10)  # exactly at the boundary: still fresh (age <= ttl)
        at_boundary = service.verify(req)
        self.assertEqual(runner.calls, 1)
        self.assertTrue(at_boundary.served_from_cache)
        clock.advance(0.001)  # now past it
        past_boundary = service.verify(req)
        self.assertEqual(runner.calls, 2)
        self.assertFalse(past_boundary.served_from_cache)

    def test_force_refresh_bypasses_cache_but_still_saves(self):
        repository = InMemoryProductVerificationRepository()
        runner = CountingRunner(sample_profile())
        service = ProductVerifierService(run_workflow=runner, repository=repository)
        req = request()
        service.verify(req)
        refreshed = service.verify(request(force_refresh=True))
        self.assertEqual(runner.calls, 2)
        self.assertFalse(refreshed.served_from_cache)
        # A later, normal request now benefits from the refreshed entry.
        third = service.verify(req)
        self.assertEqual(runner.calls, 2)
        self.assertTrue(third.served_from_cache)

    def test_no_repository_means_no_caching_at_all(self):
        runner = CountingRunner(sample_profile())
        service = ProductVerifierService(run_workflow=runner)  # repository=None
        req = request()
        service.verify(req)
        result = service.verify(req)
        self.assertEqual(runner.calls, 2)
        self.assertFalse(result.served_from_cache)


class CacheKeyTests(unittest.TestCase):
    def test_key_normalizes_whitespace_and_case(self):
        repository = InMemoryProductVerificationRepository()
        runner = CountingRunner(sample_profile())
        service = ProductVerifierService(run_workflow=runner, repository=repository)
        service.verify(request(brand="Acme", model="X100"))
        result = service.verify(request(brand="  acme ", model="x100"))
        self.assertEqual(runner.calls, 1)
        self.assertTrue(result.served_from_cache)

    def test_different_market_does_not_share_a_cache_entry(self):
        repository = InMemoryProductVerificationRepository()
        runner = CountingRunner(sample_profile())
        service = ProductVerifierService(run_workflow=runner, repository=repository)
        service.verify(request(market="global"))
        result = service.verify(request(market="US"))
        self.assertEqual(runner.calls, 2)
        self.assertFalse(result.served_from_cache)

    def test_different_article_does_not_share_a_cache_entry(self):
        repository = InMemoryProductVerificationRepository()
        runner = CountingRunner(sample_profile())
        service = ProductVerifierService(run_workflow=runner, repository=repository)
        service.verify(request(article="ABC-1"))
        result = service.verify(request(article="ABC-2"))
        self.assertEqual(runner.calls, 2)
        self.assertFalse(result.served_from_cache)

    def test_different_max_sources_does_not_share_a_cache_entry(self):
        repository = InMemoryProductVerificationRepository()
        runner = CountingRunner(sample_profile())
        service = ProductVerifierService(run_workflow=runner, repository=repository)
        service.verify(request(max_sources=5))
        result = service.verify(request(max_sources=8))
        self.assertEqual(runner.calls, 2)
        self.assertFalse(result.served_from_cache)

    def test_different_targeted_search_flag_does_not_share_a_cache_entry(self):
        repository = InMemoryProductVerificationRepository()
        runner = CountingRunner(sample_profile())
        service = ProductVerifierService(run_workflow=runner, repository=repository)
        service.verify(request(targeted_search_enabled=True))
        result = service.verify(request(targeted_search_enabled=False))
        self.assertEqual(runner.calls, 2)
        self.assertFalse(result.served_from_cache)


class FailurePolicyTests(unittest.TestCase):
    def test_invalid_request_is_never_cached(self):
        repository = InMemoryProductVerificationRepository()
        service = ProductVerifierService(run_workflow=fake_runner(None), repository=repository)
        service.verify(request(brand=""))
        service.verify(request(brand=""))
        self.assertEqual(len(repository._entries), 0)

    def test_workflow_failure_is_not_cached_as_success(self):
        repository = InMemoryProductVerificationRepository()
        runner_error = raising_runner(RuntimeError("network down"))
        calls = {"n": 0}

        def counting(req_):
            calls["n"] += 1
            return runner_error(req_)

        service = ProductVerifierService(run_workflow=counting, repository=repository)
        req = request()
        first = service.verify(req)
        second = service.verify(req)
        self.assertFalse(first.success)
        self.assertFalse(second.success)
        self.assertEqual(calls["n"], 2)  # never served a cached failure
        self.assertEqual(len(repository._entries), 0)

    def test_repository_get_failure_degrades_to_a_live_run(self):
        repository = RaisingRepository(fail_get=True)
        runner = CountingRunner(sample_profile())
        service = ProductVerifierService(run_workflow=runner, repository=repository)
        result = service.verify(request())
        self.assertTrue(result.success)
        self.assertFalse(result.served_from_cache)
        self.assertEqual(runner.calls, 1)

    def test_repository_save_failure_does_not_break_a_successful_result(self):
        repository = RaisingRepository(fail_save=True)
        runner = CountingRunner(sample_profile())
        service = ProductVerifierService(run_workflow=runner, repository=repository)
        result = service.verify(request())
        self.assertTrue(result.success)
        self.assertEqual(repository.save_calls, 1)


class CorruptionAndVersioningTests(unittest.TestCase):
    def test_corrupted_payload_falls_back_to_a_live_workflow(self):
        repository = InMemoryProductVerificationRepository()
        req = request()
        key = _cache_key(req)
        repository.save(CacheEntry(
            key=key, schema_version=1, stored_at=0.0, success=True,
            payload={"success": True},  # missing every other required key
        ))
        runner = CountingRunner(sample_profile())
        service = ProductVerifierService(run_workflow=runner, repository=repository)
        result = service.verify(req)
        self.assertTrue(result.success)
        self.assertFalse(result.served_from_cache)
        self.assertEqual(runner.calls, 1)

    def test_unsupported_schema_version_falls_back_to_a_live_workflow(self):
        repository = InMemoryProductVerificationRepository()
        req = request()
        key = _cache_key(req)
        clock = FakeClock()
        service = ProductVerifierService(
            run_workflow=CountingRunner(sample_profile()), repository=repository, clock=clock,
        )
        # Save a real, well-formed result at a future/unsupported version.
        good = service.verify(req)
        repository.save(CacheEntry(
            key=key, schema_version=999, stored_at=clock.now,
            success=True, payload=good.to_dict(),
        ))
        runner = CountingRunner(sample_profile())
        service2 = ProductVerifierService(run_workflow=runner, repository=repository)
        result = service2.verify(req)
        self.assertTrue(result.success)
        self.assertFalse(result.served_from_cache)
        self.assertEqual(runner.calls, 1)


class RoundTripTests(unittest.TestCase):
    def test_full_dto_round_trips_through_the_cache(self):
        profile = sample_profile()
        repository = InMemoryProductVerificationRepository()
        service = ProductVerifierService(run_workflow=fake_runner(profile), repository=repository)
        req = request()
        original = service.verify(req)
        cached = service.verify(req)
        self.assertTrue(cached.served_from_cache)
        self.assertEqual(cached.identity.to_dict(), original.identity.to_dict())
        self.assertEqual(cached.category.to_dict(), original.category.to_dict())
        self.assertEqual(
            [a.to_dict() for a in cached.attributes],
            [a.to_dict() for a in original.attributes],
        )
        self.assertEqual(cached.quality.to_dict(), original.quality.to_dict())
        self.assertEqual(dict(cached.metadata), dict(original.metadata))

    def test_provenance_and_evidence_round_trip(self):
        profile = sample_profile()
        repository = InMemoryProductVerificationRepository()
        service = ProductVerifierService(run_workflow=fake_runner(profile), repository=repository)
        req = request()
        service.verify(req)
        cached = service.verify(req)
        evidence = cached.attributes[0].supporting_sources[0]
        self.assertEqual(evidence.source, "https://one.example/X100")
        self.assertEqual(evidence.evidence, "power reading")
        self.assertEqual(evidence.authority_status, "verified")

    def test_service_quality_round_trips(self):
        profile = sample_profile()
        repository = InMemoryProductVerificationRepository()
        service = ProductVerifierService(run_workflow=fake_runner(profile), repository=repository)
        req = request()
        original = service.verify(req)
        cached = service.verify(req)
        self.assertEqual(cached.quality.status, original.quality.status)
        self.assertEqual(cached.quality.reasons, original.quality.reasons)
        self.assertEqual(
            cached.quality.critical_high_authority_confirmed,
            original.quality.critical_high_authority_confirmed,
        )

    def test_no_internal_dto_leaks_after_cache_restore(self):
        profile = sample_profile()
        repository = InMemoryProductVerificationRepository()
        service = ProductVerifierService(run_workflow=fake_runner(profile), repository=repository)
        req = request()
        service.verify(req)
        cached = service.verify(req)
        self.assertTrue(cached.served_from_cache)
        offenders = [node for node in _walk(cached) if isinstance(node, _INTERNAL_TYPES)]
        self.assertEqual(offenders, [])
        json.dumps(cached.to_dict())  # must not raise


class SqlitePersistenceTests(unittest.TestCase):
    """One deterministic integration test against a real, temp-file SQLite DB."""

    def test_persistence_survives_a_new_repository_instance(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "cache.sqlite3"

            repo1 = SqliteProductVerificationRepository(db_path)
            runner1 = CountingRunner(sample_profile())
            service1 = ProductVerifierService(run_workflow=runner1, repository=repo1)
            req = request()
            first = service1.verify(req)
            self.assertTrue(first.success)
            self.assertEqual(runner1.calls, 1)

            # A brand-new repository/service pointed at the same file --
            # simulates the process restarting.
            repo2 = SqliteProductVerificationRepository(db_path)

            def must_not_run(_req):
                raise AssertionError("workflow must not run on a persisted cache hit")

            service2 = ProductVerifierService(run_workflow=must_not_run, repository=repo2)
            second = service2.verify(req)
            self.assertTrue(second.success)
            self.assertTrue(second.served_from_cache)
            self.assertEqual(
                [a.to_dict() for a in second.attributes],
                [a.to_dict() for a in first.attributes],
            )

    def test_sqlite_repository_rejects_negative_ttl_policy(self):
        with self.assertRaises(ValueError):
            CachePolicy(ttl_seconds=-1)


if __name__ == "__main__":
    unittest.main()
