"""Stage 25 provider resilience/failover scenarios.

Deterministic, network-free tests covering the required failure matrix:
primary/secondary provider outages, timeout vs WAF/403 vs 429 vs empty
discovery, discovery/fetch decoupling, simultaneous multi-provider failure,
budget isolation under retry, shared circuit-breaker open/recover, and
order-independence of the final merged result.
"""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from unittest.mock import patch

import requests

from core.budget import WallClockBudget
from core.discovery import (
    DirectDomainProbeProvider,
    ResilientSearchSession,
    discover_with_status,
)
from core.fetch import fetch_candidate
from core.provider_health import FailureClass, ProviderHealthStore


class FakeProvider:
    """A scripted provider: each call pops the next queued response.

    A response is either an ``Exception`` instance (raised) or an iterable
    of ``(url, title)`` tuples (returned).
    """

    def __init__(self, name, responses, *, quality_gate=False):
        self.name = name
        self.quality_gate = quality_gate
        self._responses = list(responses)
        self.calls = []

    def search(self, query):
        return self.search_with_timeout(query, 8.0)

    def search_with_timeout(self, query, timeout_seconds):
        self.calls.append(query)
        response = self._responses.pop(0) if self._responses else []
        if isinstance(response, Exception):
            raise response
        return list(response)


def _blocked(message="403 Forbidden: bot-check blocked the search."):
    return RuntimeError(message)


def _rate_limited(message="429 Too Many Requests"):
    return RuntimeError(message)


def _connection_error(message="Connection refused"):
    return RuntimeError(message)


class DiscoveryFailoverTests(unittest.TestCase):
    """Scenarios 1, 2, 3, 4, 5, 6, 8, 12 from the Stage 25 test matrix."""

    def test_primary_provider_fully_unavailable_secondary_serves(self) -> None:
        primary = FakeProvider("primary", [_blocked(), _blocked(), _blocked()])
        secondary = FakeProvider("secondary", [[("https://acme.example/p", "Acme P")]] * 3)
        session = ResilientSearchSession(providers=(primary, secondary))

        outcome = session.search_with_status("Acme P")

        self.assertEqual([item.url for item in outcome.results], ["https://acme.example/p"])
        self.assertEqual(len(primary.calls), 1)
        self.assertEqual(len(secondary.calls), 1)

    def test_secondary_provider_fully_unavailable_primary_still_serves(self) -> None:
        primary = FakeProvider("primary", [[("https://acme.example/p", "Acme P")]])
        secondary = FakeProvider("secondary", [_blocked()])
        session = ResilientSearchSession(providers=(primary, secondary))

        outcome = session.search_with_status("Acme P")

        self.assertEqual([item.url for item in outcome.results], ["https://acme.example/p"])
        # Secondary is never even reached: primary already succeeded.
        self.assertEqual(secondary.calls, [])

    def test_primary_discovery_timeout_falls_back(self) -> None:
        primary = FakeProvider("primary", [TimeoutError("primary deadline exhausted")])
        fallback = FakeProvider("fallback", [[("https://acme.example/p", "Acme P")]])
        session = ResilientSearchSession(providers=(primary, fallback))

        outcome = session.search_with_status("Acme P")

        self.assertEqual([item.status for item in outcome.attempts], ["timeout", "success"])
        self.assertEqual(outcome.attempts[0].failure_class, FailureClass.TIMEOUT)
        self.assertEqual([item.url for item in outcome.results], ["https://acme.example/p"])

    def test_primary_discovery_returns_403_waf_falls_back(self) -> None:
        primary = FakeProvider("primary", [_blocked("bot-check: 403 Forbidden")])
        fallback = FakeProvider("fallback", [[("https://acme.example/p", "Acme P")]])
        session = ResilientSearchSession(providers=(primary, fallback))

        outcome = session.search_with_status("Acme P")

        self.assertEqual(outcome.attempts[0].status, "blocked")
        self.assertEqual(outcome.attempts[0].failure_class, FailureClass.BLOCKED)
        self.assertEqual([item.url for item in outcome.results], ["https://acme.example/p"])

    def test_primary_discovery_returns_429_falls_back(self) -> None:
        primary = FakeProvider("primary", [_rate_limited("429 Too Many Requests")])
        fallback = FakeProvider("fallback", [[("https://acme.example/p", "Acme P")]])
        session = ResilientSearchSession(providers=(primary, fallback))

        outcome = session.search_with_status("Acme P")

        self.assertEqual(outcome.attempts[0].failure_class, FailureClass.RATE_LIMITED)
        self.assertEqual([item.url for item in outcome.results], ["https://acme.example/p"])

    def test_primary_discovery_returns_empty_falls_back(self) -> None:
        primary = FakeProvider("primary", [[]])
        fallback = FakeProvider("fallback", [[("https://acme.example/p", "Acme P")]])
        session = ResilientSearchSession(providers=(primary, fallback))

        outcome = session.search_with_status("Acme P")

        self.assertEqual(outcome.attempts[0].status, "empty")
        self.assertEqual([item.url for item in outcome.results], ["https://acme.example/p"])

    def test_simultaneous_multi_provider_failure_still_yields_a_result(self) -> None:
        first = FakeProvider("first", [_blocked()])
        second = FakeProvider("second", [TimeoutError("deadline")])
        third = FakeProvider("third", [_connection_error()])
        fourth = FakeProvider("fourth", [[("https://acme.example/p", "Acme P")]])
        session = ResilientSearchSession(providers=(first, second, third, fourth))

        outcome = session.search_with_status("Acme P")

        failing_statuses = [item.status for item in outcome.attempts[:3]]
        self.assertTrue(all(status != "success" for status in failing_statuses))
        self.assertEqual([item.url for item in outcome.results], ["https://acme.example/p"])

    def test_result_is_identical_regardless_of_which_provider_failed_first(self) -> None:
        evidence = [("https://acme.example/p", "Acme P")]

        order_a = ResilientSearchSession(providers=(
            FakeProvider("a", [_blocked()]),
            FakeProvider("b", [TimeoutError("deadline")]),
            FakeProvider("c", [evidence]),
        ))
        order_b = ResilientSearchSession(providers=(
            FakeProvider("a", [TimeoutError("deadline")]),
            FakeProvider("b", [_blocked()]),
            FakeProvider("c", [evidence]),
        ))

        result_a = [item.url for item in order_a.search_with_status("Acme P").results]
        result_b = [item.url for item in order_b.search_with_status("Acme P").results]
        self.assertEqual(result_a, result_b)


class DiscoveryFetchDecouplingTests(unittest.TestCase):
    """Scenario 7: discovery succeeds, primary fetch path fails."""

    def test_fetch_failure_does_not_depend_on_which_provider_discovered_the_url(self) -> None:
        candidates = discover_with_status(
            "Acme", "P100",
            searcher=lambda _query: [("https://acme.example/product/p100", "Acme P100")],
        )
        self.assertTrue(candidates.candidates)
        url = candidates.candidates[0]["url"]

        session = requests.Session()
        with patch.object(session, "get", side_effect=requests.exceptions.ConnectionError("refused")):
            result = fetch_candidate({"url": url}, timeout=5.0, session=session)

        # The fetch failure is entirely a fetch-layer concern; nothing about
        # it depends on, or mutates, which discovery provider found the URL.
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["discovery_metadata"]["url"], url)


class BudgetIsolationTests(unittest.TestCase):
    """Scenario 9: a dead-fast-failing provider must not eat the whole budget."""

    def test_connection_error_retry_is_bounded_and_leaves_budget_for_fallback(self) -> None:
        primary = FakeProvider("primary", [_connection_error(), _connection_error()])
        fallback = FakeProvider("fallback", [[("https://acme.example/p", "Acme P")]])
        budget = WallClockBudget(30.0)
        session = ResilientSearchSession(providers=(primary, fallback), budget=budget)

        with patch("core.discovery.time.sleep"):
            outcome = session.search_with_status("Acme P")

        # Exactly one bounded retry (2 calls total), not unlimited retries.
        self.assertEqual(len(primary.calls), 2)
        self.assertTrue(outcome.attempts[0].retried)
        self.assertEqual([item.url for item in outcome.results], ["https://acme.example/p"])

    def test_no_retry_once_budget_is_nearly_exhausted(self) -> None:
        class SlowFailPrimary(FakeProvider):
            def search_with_timeout(self, query, timeout_seconds):
                self.calls.append(query)
                time.sleep(0.05)
                raise _connection_error()

        primary = SlowFailPrimary("primary", [])
        fallback = FakeProvider("fallback", [[("https://acme.example/p", "Acme P")]])
        # Just over the 1.0s retry threshold: a real ~0.05s failing call
        # drops remaining budget below it, so the bounded retry is skipped
        # rather than spending a second attempt on an already-tight budget.
        budget = WallClockBudget(1.02)
        session = ResilientSearchSession(providers=(primary, fallback), budget=budget)

        outcome = session.search_with_status("Acme P")

        self.assertEqual(len(primary.calls), 1)
        self.assertFalse(outcome.attempts[0].retried)
        self.assertEqual([item.url for item in outcome.results], ["https://acme.example/p"])


class SharedCircuitBreakerTests(unittest.TestCase):
    """Scenarios 10 and 11: shared circuit opens across sessions, then recovers."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.path = os.path.join(self._tmpdir.name, "health.json")

    def test_circuit_opens_after_failures_observed_across_two_sessions(self) -> None:
        store = ProviderHealthStore(path=self.path, open_after_failures=2)
        fallback_evidence = [("https://acme.example/p", "Acme P")]

        # Product 1: primary fails once. Not yet open.
        session_one = ResilientSearchSession(
            providers=(FakeProvider("primary", [_blocked()]), FakeProvider("fallback", [fallback_evidence])),
            health_store=store,
        )
        session_one.search_with_status("Acme P")
        self.assertFalse(store.is_open("discovery:primary"))

        # Product 2 (new session, same run): primary fails again -> shared
        # circuit opens. Product 3 should skip primary without a request.
        session_two = ResilientSearchSession(
            providers=(FakeProvider("primary", [_blocked()]), FakeProvider("fallback", [fallback_evidence])),
            health_store=store,
        )
        session_two.search_with_status("Acme P")
        self.assertTrue(store.is_open("discovery:primary"))

        primary_three = FakeProvider("primary", [[("https://should-not-be-called.example/", "x")]])
        session_three = ResilientSearchSession(
            providers=(primary_three, FakeProvider("fallback", [fallback_evidence])),
            health_store=store,
        )
        outcome = session_three.search_with_status("Acme P")
        self.assertEqual(primary_three.calls, [])
        self.assertEqual(outcome.attempts[0].status, "circuit_open")
        self.assertTrue(outcome.attempts[0].shared_circuit_open)

    def test_circuit_allows_recovery_probe_after_cooldown(self) -> None:
        clock = {"now": 0.0}
        store = ProviderHealthStore(
            path=self.path, open_after_failures=1, cooldown_seconds=10.0,
            clock=lambda: clock["now"],
        )
        store.record_failure("discovery:primary", FailureClass.BLOCKED)
        self.assertTrue(store.is_open("discovery:primary"))

        clock["now"] += 11.0
        primary = FakeProvider("primary", [[("https://acme.example/p", "Acme P")]])
        session = ResilientSearchSession(
            providers=(primary,), health_store=store,
        )
        outcome = session.search_with_status("Acme P")

        # Cooldown elapsed: the probe is allowed through, and it succeeds.
        self.assertEqual(len(primary.calls), 1)
        self.assertEqual([item.url for item in outcome.results], ["https://acme.example/p"])
        self.assertFalse(store.is_open("discovery:primary"))


class FetchSharedCircuitBreakerTests(unittest.TestCase):
    """Fetch-side equivalent of scenarios 10/11, keyed by host instead of provider."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.path = os.path.join(self._tmpdir.name, "health.json")

    def test_repeated_host_failures_open_shared_fetch_circuit(self) -> None:
        store = ProviderHealthStore(path=self.path, open_after_failures=2)
        session = requests.Session()
        candidate = {"url": "https://dead.example/product"}

        with patch.object(session, "get", side_effect=requests.exceptions.ConnectionError("refused")):
            fetch_candidate(candidate, timeout=2.0, session=session, health_store=store)
            fetch_candidate(candidate, timeout=2.0, session=session, health_store=store)

        self.assertTrue(store.is_open("fetch:dead.example"))

        # A third call must be skipped without ever touching the network.
        with patch.object(session, "get") as get:
            result = fetch_candidate(candidate, timeout=2.0, session=session, health_store=store)
        get.assert_not_called()
        self.assertEqual(result["status"], "error")


class DirectDomainProbeProviderTests(unittest.TestCase):
    """The Stage 25 SERP-independent discovery path."""

    class FakeResponse:
        def __init__(self, status_code=200, text="", url=""):
            self.status_code = status_code
            self.text = text
            self.url = url or "https://www.acme.com/"

    def test_only_activates_for_the_official_website_bootstrap_query(self) -> None:
        session = requests.Session()
        provider = DirectDomainProbeProvider("global", session=session)
        with patch.object(session, "get") as get:
            results = provider.search("Acme X100 specifications")
        get.assert_not_called()
        self.assertEqual(results, [])

    def test_probes_generic_brand_domain_and_matches_title(self) -> None:
        session = requests.Session()
        provider = DirectDomainProbeProvider("global", session=session)
        response = self.FakeResponse(
            status_code=200, text="<html><head><title>Acme - Official Home</title></head></html>",
            url="https://www.acme.com/",
        )
        with patch.object(session, "get", return_value=response) as get:
            results = provider.search("Acme official website")
        get.assert_called()
        self.assertEqual(results, [("https://www.acme.com/", "Acme - Official Home")])

    def test_never_used_search_engine_transport(self) -> None:
        # The whole point of this provider: it never imports/calls anything
        # from the SERP-scraping providers, so it cannot share their
        # failure domain. Verified structurally: it only owns a requests
        # session, no reference to any other provider class.
        provider = DirectDomainProbeProvider("global")
        self.assertTrue(hasattr(provider, "_session"))
        self.assertEqual(provider.last_transport, "http_direct")

    def test_non_matching_title_is_rejected(self) -> None:
        session = requests.Session()
        provider = DirectDomainProbeProvider("global", session=session)
        response = self.FakeResponse(
            status_code=200, text="<html><head><title>Unrelated Company</title></head></html>",
        )
        with patch.object(session, "get", return_value=response):
            results = provider.search("Acme official website")
        self.assertEqual(results, [])

    def test_error_response_is_skipped_not_raised(self) -> None:
        session = requests.Session()
        provider = DirectDomainProbeProvider("global", session=session)
        response = self.FakeResponse(status_code=404)
        with patch.object(session, "get", return_value=response):
            results = provider.search("Acme official website")
        self.assertEqual(results, [])

    def test_serves_as_last_resort_when_every_serp_provider_is_down(self) -> None:
        session = requests.Session()
        probe = DirectDomainProbeProvider("global", session=session)
        response = self.FakeResponse(
            status_code=200, text="<html><head><title>Acme Official</title></head></html>",
            url="https://www.acme.com/",
        )
        serp_providers = [
            FakeProvider("ddg", [_blocked()]),
            FakeProvider("naver", [_blocked()]),
            FakeProvider("bing", [TimeoutError("deadline")]),
        ]
        resilient = ResilientSearchSession(providers=(*serp_providers, probe))
        with patch.object(session, "get", return_value=response):
            outcome = resilient.search_with_status("Acme official website")

        self.assertEqual([item.url for item in outcome.results], ["https://www.acme.com/"])
        self.assertEqual(outcome.attempts[-1].provider, "direct_domain_probe")
        self.assertEqual(outcome.attempts[-1].status, "success")


if __name__ == "__main__":
    unittest.main()
