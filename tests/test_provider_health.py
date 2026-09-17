"""Unit tests for the cross-process provider health/circuit-breaker store."""

from __future__ import annotations

import os
import tempfile
import unittest

from core.provider_health import FailureClass, ProviderHealthStore


class DisabledStoreTests(unittest.TestCase):
    def test_disabled_store_never_opens(self) -> None:
        store = ProviderHealthStore.disabled()
        for _ in range(10):
            store.record_failure("discovery:bing", FailureClass.BLOCKED)
        self.assertFalse(store.is_open("discovery:bing"))

    def test_from_env_without_var_is_disabled(self) -> None:
        os.environ.pop("PDV_PROVIDER_HEALTH_PATH", None)
        store = ProviderHealthStore.from_env()
        self.assertFalse(store.enabled)


class InMemoryHealthStoreTests(unittest.TestCase):
    def test_opens_after_configured_consecutive_failures(self) -> None:
        store = ProviderHealthStore(path=None, open_after_failures=2)
        self.assertFalse(store.is_open("discovery:naver"))
        store.record_failure("discovery:naver", FailureClass.BLOCKED)
        self.assertFalse(store.is_open("discovery:naver"))
        store.record_failure("discovery:naver", FailureClass.BLOCKED)
        self.assertTrue(store.is_open("discovery:naver"))

    def test_success_resets_consecutive_failures(self) -> None:
        store = ProviderHealthStore(path=None, open_after_failures=2)
        store.record_failure("discovery:naver", FailureClass.TIMEOUT)
        store.record_success("discovery:naver")
        store.record_failure("discovery:naver", FailureClass.TIMEOUT)
        self.assertFalse(store.is_open("discovery:naver"))

    def test_cooldown_moves_open_to_half_open(self) -> None:
        clock = {"now": 1000.0}
        store = ProviderHealthStore(
            path=None, open_after_failures=1,
            cooldown_seconds=10.0, clock=lambda: clock["now"],
        )
        store.record_failure("discovery:ddg", FailureClass.BLOCKED)
        self.assertTrue(store.is_open("discovery:ddg"))
        clock["now"] += 5.0
        self.assertTrue(store.is_open("discovery:ddg"))
        clock["now"] += 10.0
        self.assertFalse(store.is_open("discovery:ddg"))

    def test_half_open_probe_failure_extends_cooldown(self) -> None:
        clock = {"now": 0.0}
        store = ProviderHealthStore(
            path=None, open_after_failures=1,
            cooldown_seconds=5.0, max_cooldown_seconds=40.0,
            clock=lambda: clock["now"],
        )
        store.record_failure("fetch:example.com", FailureClass.TIMEOUT)
        clock["now"] += 6.0
        self.assertFalse(store.is_open("fetch:example.com"))
        store.record_failure("fetch:example.com", FailureClass.TIMEOUT)
        self.assertTrue(store.is_open("fetch:example.com"))
        clock["now"] += 6.0
        self.assertTrue(store.is_open("fetch:example.com"))

    def test_half_open_probe_success_closes_circuit(self) -> None:
        clock = {"now": 0.0}
        store = ProviderHealthStore(
            path=None, open_after_failures=2,
            cooldown_seconds=5.0, clock=lambda: clock["now"],
        )
        store.record_failure("discovery:bing", FailureClass.BLOCKED)
        store.record_failure("discovery:bing", FailureClass.BLOCKED)
        self.assertTrue(store.is_open("discovery:bing"))
        clock["now"] += 6.0
        self.assertFalse(store.is_open("discovery:bing"))
        store.record_success("discovery:bing")
        self.assertFalse(store.is_open("discovery:bing"))
        # A single failure right after a successful probe closed the
        # circuit should not immediately reopen it (still below threshold).
        store.record_failure("discovery:bing", FailureClass.BLOCKED)
        self.assertFalse(store.is_open("discovery:bing"))


class FileBackedHealthStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.path = os.path.join(self._tmpdir.name, "health.json")

    def test_state_shared_across_store_instances(self) -> None:
        writer = ProviderHealthStore(path=self.path, open_after_failures=2)
        writer.record_failure("discovery:duckduckgo_html", FailureClass.BLOCKED)
        writer.record_failure("discovery:duckduckgo_html", FailureClass.BLOCKED)
        reader = ProviderHealthStore(path=self.path, open_after_failures=2)
        self.assertTrue(reader.is_open("discovery:duckduckgo_html"))

    def test_two_independent_products_open_circuit_together(self) -> None:
        # Simulates two concurrent product runs (separate processes in
        # production) each seeing one failure of the same provider; neither
        # alone should open the shared circuit, but together they should.
        product_one = ProviderHealthStore(path=self.path, open_after_failures=2)
        product_two = ProviderHealthStore(path=self.path, open_after_failures=2)
        product_one.record_failure("discovery:naver", FailureClass.BLOCKED)
        self.assertFalse(product_two.is_open("discovery:naver"))
        product_two.record_failure("discovery:naver", FailureClass.BLOCKED)
        self.assertTrue(product_one.is_open("discovery:naver"))
        self.assertTrue(product_two.is_open("discovery:naver"))

    def test_snapshot_reports_recorded_keys(self) -> None:
        store = ProviderHealthStore(path=self.path, open_after_failures=2)
        store.record_success("discovery:bing")
        store.record_failure("fetch:example.com", FailureClass.CONNECTION_ERROR)
        snapshot = store.snapshot()
        self.assertIn("discovery:bing", snapshot)
        self.assertIn("fetch:example.com", snapshot)
        self.assertEqual(snapshot["discovery:bing"]["successes"], 1)
        self.assertEqual(snapshot["fetch:example.com"]["failures"], 1)

    def test_reset_clears_persisted_state(self) -> None:
        store = ProviderHealthStore(path=self.path, open_after_failures=1)
        store.record_failure("discovery:google", FailureClass.TIMEOUT)
        self.assertTrue(store.is_open("discovery:google"))
        store.reset()
        fresh = ProviderHealthStore(path=self.path, open_after_failures=1)
        self.assertFalse(fresh.is_open("discovery:google"))

    def test_missing_file_is_treated_as_empty(self) -> None:
        missing_path = os.path.join(self._tmpdir.name, "does-not-exist.json")
        store = ProviderHealthStore(path=missing_path, open_after_failures=1)
        self.assertFalse(store.is_open("discovery:bing"))

    def test_parent_directory_is_created_automatically(self) -> None:
        nested_path = os.path.join(self._tmpdir.name, "nested", "sub", "health.json")
        store = ProviderHealthStore(path=nested_path, open_after_failures=1)
        store.record_failure("discovery:bing", FailureClass.BLOCKED)
        self.assertTrue(store.is_open("discovery:bing"))
        self.assertTrue(os.path.exists(nested_path))


if __name__ == "__main__":
    unittest.main()
