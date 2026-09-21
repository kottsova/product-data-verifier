"""Stage 36.7: a real external limit for one discovery call.

These tests run genuine child processes (hung provider, endless page load, a
browser that never closes, concurrent requests) and assert on measured elapsed
time, process survival and the partial result - not on mocks of the limiter.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import tempfile
import time
import unittest

from core.discovery import canonicalize_url
from core.process_containment import pid_alive
from services.discovery_debug import DiscoveryDebugService
from services.discovery_isolation import ReplayState
from tests.isolation_hooks import EXACT_URL as _RAW_EXACT_URL

EXACT_URL = canonicalize_url(_RAW_EXACT_URL)  # discovery drops the leading www.

HOOKS = "tests.isolation_hooks:"
# Documented allowance beyond the budget: grace (kill + result cut-over) plus
# a small replay/IO slack.  The production defaults are 75 s + 5 s grace.
REPLAY_SLACK_SECONDS = 1.5


def _urls(result) -> set[str]:
    groups = (*result.official, *result.dealers, *result.secondary, *result.rejected)
    return {canonicalize_url(item.url) for item in groups}


class _PidFile:
    def __enter__(self):
        handle, self.path = tempfile.mkstemp(suffix=".pids")
        os.close(handle)
        os.environ["PDV_TEST_PIDFILE"] = self.path
        return self

    def __exit__(self, *_):
        os.environ.pop("PDV_TEST_PIDFILE", None)
        Path(self.path).unlink(missing_ok=True)

    def pids(self) -> list[int]:
        text = Path(self.path).read_text(encoding="utf-8")
        return sorted({int(line.split()[1]) for line in text.splitlines() if line.strip()})


def _service(scenario: str, budget: float, grace: float) -> DiscoveryDebugService:
    return DiscoveryDebugService(
        wall_clock_budget_seconds=budget, hard_stop_grace_seconds=grace,
        worker_hooks=HOOKS + scenario,
    )


class HardDeadlineTests(unittest.TestCase):
    def assertNoSurvivors(self, result, pidfile: _PidFile | None = None) -> None:
        isolation = result.performance["isolation"]
        self.assertEqual(isolation["leftover_process_count"], 0)
        for pid in isolation["contained_pids"]:
            self.assertFalse(pid_alive(pid), f"process {pid} survived")
        if pidfile is not None:
            for pid in pidfile.pids():
                self.assertFalse(pid_alive(pid), f"test process {pid} survived")

    def test_hung_provider_is_stopped_with_partial_result(self) -> None:
        budget, grace = 5.0, 2.0
        with _PidFile() as pidfile:
            started = time.monotonic()
            result = _service("hang_in_serp", budget, grace).discover_name("Bosch WAN28254GB")
            elapsed = time.monotonic() - started
            self.assertLessEqual(elapsed, budget + grace + REPLAY_SLACK_SECONDS)
            self.assertGreaterEqual(elapsed, budget)  # it really waited for the budget
            isolation = result.performance["isolation"]
            self.assertTrue(isolation["hard_stop"])
            self.assertEqual(isolation["stop_reason"], "hard_deadline")
            self.assertEqual(isolation["stopped_in"]["kind"], "provider")
            self.assertEqual(isolation["stopped_in"]["target"], "hanging_serp")
            self.assertIn(EXACT_URL, _urls(result))  # found before the stall, kept
            self.assertTrue(any(
                item["provider"] == "hanging_serp" and item["status"] == "timeout"
                for item in result.provider_failures
            ))
            self.assertLessEqual(result.performance["budget_overrun_seconds"],
                                 grace + REPLAY_SLACK_SECONDS)
            self.assertFalse(isolation["worker_stderr_epipe"])
            self.assertNoSurvivors(result, pidfile)

    def test_endless_page_load_is_stopped_and_reported(self) -> None:
        budget, grace = 12.0, 2.0  # > the 8 s minimum needed to start a page fetch
        started = time.monotonic()
        result = _service("slow_page_load", budget, grace).discover_name("Bosch WAN28254GB")
        elapsed = time.monotonic() - started
        self.assertLessEqual(elapsed, budget + grace + REPLAY_SLACK_SECONDS)
        isolation = result.performance["isolation"]
        self.assertTrue(isolation["hard_stop"])
        self.assertEqual(isolation["stopped_in"]["kind"], "page_fetch")
        self.assertIn(EXACT_URL, isolation["stopped_in"]["target"])
        self.assertIn(EXACT_URL, _urls(result))
        self.assertEqual([item["status"] for item in result.page_fetches], ["hard_deadline"])
        self.assertNoSurvivors(result)

    def test_slow_cleanup_does_not_delay_a_finished_result(self) -> None:
        budget, grace = 12.0, 2.0
        with _PidFile() as pidfile:
            started = time.monotonic()
            result = _service("slow_cleanup", budget, grace).discover_name("Bosch WAN28254GB")
            elapsed = time.monotonic() - started
            # __exit__ sleeps 600 s: the wait is capped by the exit allowance.
            self.assertLess(elapsed, budget)
            isolation = result.performance["isolation"]
            self.assertFalse(isolation["hard_stop"])
            self.assertTrue(isolation["result_complete"])
            self.assertEqual(isolation["stop_reason"], "cleanup_exceeded_grace")
            self.assertIn(EXACT_URL, _urls(result))
            self.assertFalse(isolation["worker_stderr_epipe"])
            self.assertNoSurvivors(result, pidfile)
            self.assertGreaterEqual(len(pidfile.pids()), 2)  # worker + stray child were tracked

    def test_real_chromium_with_stuck_shutdown_leaves_no_process(self) -> None:
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as pw:
                pw.chromium.launch(headless=True).close()
        except Exception as error:  # noqa: BLE001
            self.skipTest(f"Playwright Chromium unavailable: {error}")
        started = time.monotonic()
        result = _service("real_chromium_slow_close", 12.0, 2.0).discover_name("Bosch WAN28254GB")
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 12.0)
        isolation = result.performance["isolation"]
        self.assertTrue(isolation["result_complete"])
        self.assertGreaterEqual(isolation["contained_process_count"], 3)  # worker, driver, browser
        self.assertFalse(isolation["worker_stderr_epipe"])
        self.assertNoSurvivors(result)

    def test_concurrent_requests_are_bounded_independently(self) -> None:
        budget, grace, count = 5.0, 2.0, 4
        service = _service("hang_in_serp", budget, grace)
        started = time.monotonic()
        with ThreadPoolExecutor(max_workers=count) as pool:
            results = list(pool.map(
                lambda _: service.discover_name("Bosch WAN28254GB"), range(count),
            ))
        elapsed = time.monotonic() - started
        # Parallel, not serialised: far below count * (budget + grace).
        self.assertLessEqual(elapsed, budget + grace + 2 * REPLAY_SLACK_SECONDS)
        for result in results:
            isolation = result.performance["isolation"]
            self.assertTrue(isolation["hard_stop"])
            self.assertIn(EXACT_URL, _urls(result))
            self.assertNoSurvivors(result)
        pids = [pid for result in results for pid in result.performance["isolation"]["contained_pids"]]
        self.assertEqual(len(pids), len(set(pids)))  # each request had its own tree

    def test_worker_crash_is_a_structured_result(self) -> None:
        result = _service("crash_in_worker", 10.0, 2.0).discover_name("Bosch WAN28254GB")
        isolation = result.performance["isolation"]
        self.assertTrue(isolation["hard_stop"])
        self.assertEqual(isolation["stop_reason"], "worker_exited_without_result")
        self.assertIn("worker hook failed on purpose", isolation["worker_error"])
        self.assertEqual(isolation["worker_exit_code"], 3)
        self.assertNoSurvivors(result)

    def test_healthy_run_is_complete_and_matches_the_inline_pipeline(self) -> None:
        isolated = _service("fast_only", 20.0, 3.0).discover_name("Bosch WAN28254GB")
        from tests.isolation_hooks import FastProvider
        inline = DiscoveryDebugService(
            wall_clock_budget_seconds=20.0, providers=lambda: [FastProvider()],
        ).discover_name("Bosch WAN28254GB")
        self.assertTrue(isolated.performance["isolation"]["result_complete"])
        self.assertFalse(isolated.performance["isolation"]["hard_stop"])
        self.assertEqual((isolated.status, isolated.exact_official_found),
                         (inline.status, inline.exact_official_found))
        self.assertEqual(_urls(isolated), _urls(inline))


class ConfigurationTests(unittest.TestCase):
    def test_public_route_is_isolated_by_default(self) -> None:
        self.assertTrue(DiscoveryDebugService().isolated)
        self.assertEqual(DiscoveryDebugService().hard_stop_grace_seconds, 5.0)

    def test_injected_providers_stay_inline_unless_hooks_rebuild_them(self) -> None:
        self.assertFalse(DiscoveryDebugService(providers=lambda: []).isolated)
        with self.assertRaises(ValueError):
            DiscoveryDebugService(providers=lambda: [], isolation="process")

    def test_missing_identity_still_raises_before_any_process(self) -> None:
        with self.assertRaises(ValueError):
            DiscoveryDebugService().discover_name("   ")


class ReplayStateTests(unittest.TestCase):
    def test_stalled_call_prefers_open_page_fetch_then_provider(self) -> None:
        state = ReplayState()
        state.apply({"e": "session", "t": 10.0, "kind": "query_start", "payload": ("q", ())})
        state.apply({"e": "session", "t": 11.0, "kind": "provider_start", "payload": ("bing", 8.0)})
        stalled = state.stalled_call(now=30.0)
        self.assertEqual((stalled["kind"], stalled["target"], stalled["stalled_seconds"]),
                         ("provider", "bing", 19.0))
        state.apply({"e": "fetch_start", "t": 20.0, "url": "https://example.test/p"})
        self.assertEqual(state.stalled_call(now=30.0)["kind"], "page_fetch")

    def test_completed_query_is_replayed_and_unknown_query_is_budget_stop(self) -> None:
        state = ReplayState()
        state.apply({"e": "session", "t": 1.0, "kind": "query_start", "payload": ("q", ())})
        state.apply({"e": "session", "t": 1.1, "kind": "results", "payload": [("https://a.test/x", "t")]})
        state.apply({"e": "session", "t": 1.2, "kind": "query_end", "payload": None})
        session = state.session()
        self.assertEqual(session.search_with_status("q").results, (("https://a.test/x", "t"),))
        stop = session.search_with_status("other")
        self.assertTrue(stop.attempts[0].budget_exhausted)
        self.assertEqual(stop.attempts[0].failure_reason, "hard_deadline")


if __name__ == "__main__":
    unittest.main()
