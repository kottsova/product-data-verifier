"""Cross-process provider health tracking shared across one live run.

``core.discovery.ResilientSearchSession`` and ``core.fetch.fetch_candidate``
already isolate one provider's failure from the current product (a request-
local circuit breaker and per-provider time-share cap, see
``core.discovery.DiscoveryRuntimeConfig``). Neither mechanism, however,
survives past the current product: each live product run builds a fresh
session (and, under the regression/diagnostics multiprocessing scheduler,
runs in its own OS process), so a provider that is globally blocked (WAF,
rate limit) gets rediscovered from scratch, at full cost, by every remaining
product in the same blind run.

This module adds one more, intentionally small layer: a health store that
several products in the same run can share. It is opt-in (disabled unless a
store path is configured) and purely a scheduling optimization -- it decides
which provider to *try first*, never which evidence to trust. Authority,
identity, and validation are untouched by anything in this file.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
import tempfile
import time
from typing import Mapping


class FailureClass:
    """Coarse, generic failure buckets shared by discovery and fetch."""

    TIMEOUT = "timeout"
    CONNECTION_ERROR = "connection_error"
    BLOCKED = "blocked"
    RATE_LIMITED = "rate_limited"
    EMPTY = "empty"
    MALFORMED = "malformed"
    UNAVAILABLE = "unavailable"
    OTHER = "other"


# Failures worth one bounded local retry: fast, plausibly transient network
# conditions. "blocked" (WAF/403) and "malformed" are deliberately excluded --
# retrying a WAF challenge or a parse failure just repeats the same outcome
# and burns budget, per the Stage 24 finding that a dead provider must not
# monopolize the shared workflow budget.
RETRYABLE_FAILURE_CLASSES = frozenset({
    FailureClass.TIMEOUT,
    FailureClass.CONNECTION_ERROR,
    FailureClass.RATE_LIMITED,
    FailureClass.UNAVAILABLE,
})

# Failures that count against a provider's shared health. "empty" and
# "malformed"-as-low-value are not included: a provider that is reachable
# but topically unhelpful for one query is not "unhealthy" the way a
# WAF block or timeout is, and must not be starved for the rest of the run.
HEALTH_AFFECTING_FAILURE_CLASSES = frozenset({
    FailureClass.TIMEOUT,
    FailureClass.CONNECTION_ERROR,
    FailureClass.BLOCKED,
    FailureClass.RATE_LIMITED,
    FailureClass.UNAVAILABLE,
})

ENV_VAR = "PDV_PROVIDER_HEALTH_PATH"

DEFAULT_OPEN_AFTER_FAILURES = 2
DEFAULT_COOLDOWN_SECONDS = 45.0
DEFAULT_MAX_COOLDOWN_SECONDS = 180.0


@dataclass
class _EntryState:
    consecutive_failures: int = 0
    consecutive_successes: int = 0
    successes: int = 0
    failures: int = 0
    state: str = "closed"  # closed | open | half_open
    opened_at: float = 0.0
    cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS
    last_failure_class: str | None = None
    last_updated: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, object]:
        return {
            "consecutive_failures": self.consecutive_failures,
            "consecutive_successes": self.consecutive_successes,
            "successes": self.successes,
            "failures": self.failures,
            "state": self.state,
            "opened_at": self.opened_at,
            "cooldown_seconds": self.cooldown_seconds,
            "last_failure_class": self.last_failure_class,
            "last_updated": self.last_updated,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "_EntryState":
        entry = cls()
        entry.consecutive_failures = int(data.get("consecutive_failures", 0))  # type: ignore[arg-type]
        entry.consecutive_successes = int(data.get("consecutive_successes", 0))  # type: ignore[arg-type]
        entry.successes = int(data.get("successes", 0))  # type: ignore[arg-type]
        entry.failures = int(data.get("failures", 0))  # type: ignore[arg-type]
        entry.state = str(data.get("state", "closed"))
        entry.opened_at = float(data.get("opened_at", 0.0))  # type: ignore[arg-type]
        entry.cooldown_seconds = float(data.get("cooldown_seconds", DEFAULT_COOLDOWN_SECONDS))  # type: ignore[arg-type]
        last_failure_class = data.get("last_failure_class")
        entry.last_failure_class = str(last_failure_class) if last_failure_class else None
        entry.last_updated = float(data.get("last_updated", time.time()))  # type: ignore[arg-type]
        return entry


class _FileLock:
    """Best-effort advisory lock via an exclusive-create sidecar file.

    Health tracking is a scheduling optimization, not a correctness
    guarantee, so a lock that cannot be acquired promptly is treated as
    "skip this update" rather than blocking the pipeline.
    """

    def __init__(self, path: str, *, timeout: float = 2.0) -> None:
        self._lock_path = f"{path}.lock"
        self._timeout = timeout
        self._acquired = False

    def __enter__(self) -> bool:
        deadline = time.monotonic() + self._timeout
        while time.monotonic() < deadline:
            try:
                fd = os.open(self._lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.close(fd)
                self._acquired = True
                return True
            except FileExistsError:
                try:
                    if time.time() - os.path.getmtime(self._lock_path) > self._timeout:
                        os.remove(self._lock_path)
                        continue
                except OSError:
                    pass
                time.sleep(0.02)
            except OSError:
                # e.g. the parent directory vanished mid-run. Health
                # tracking is best-effort, so give up on this update rather
                # than raise into the pipeline.
                return False
        return False

    def __exit__(self, *args: object) -> None:
        if self._acquired:
            try:
                os.remove(self._lock_path)
            except OSError:
                pass


class ProviderHealthStore:
    """Shared success/failure counters and a simple circuit breaker per key.

    Keys are free-form strings (``"discovery:bing"``, ``"fetch:example.com"``)
    so discovery providers and fetch hosts share one mechanism without being
    conflated with each other.
    """

    def __init__(
        self,
        path: str | None = None,
        *,
        open_after_failures: int = DEFAULT_OPEN_AFTER_FAILURES,
        cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS,
        max_cooldown_seconds: float = DEFAULT_MAX_COOLDOWN_SECONDS,
        active: bool = True,
        clock: "object" = time.time,
    ) -> None:
        self._path = path
        self._open_after_failures = max(1, int(open_after_failures))
        self._cooldown_seconds = float(cooldown_seconds)
        self._max_cooldown_seconds = float(max_cooldown_seconds)
        # "active" means this instance tracks circuit state at all (a fully
        # disabled store is a pure no-op, used as the safe default). Among
        # active stores, a path means state is also persisted so other
        # processes/products in the same run observe it; without one, state
        # stays in-memory for this instance only (useful in single-process
        # tests).
        self._active = active
        self._enabled = active and path is not None
        self._clock = clock
        self._memory: dict[str, _EntryState] = {}
        if self._enabled:
            assert path is not None
            directory = os.path.dirname(path) or "."
            os.makedirs(directory, exist_ok=True)

    @classmethod
    def disabled(cls) -> "ProviderHealthStore":
        return cls(path=None, active=False)

    @classmethod
    def from_env(cls, env_var: str = ENV_VAR) -> "ProviderHealthStore":
        path = os.environ.get(env_var)
        if not path:
            return cls.disabled()
        return cls(path=path)

    @property
    def enabled(self) -> bool:
        return self._active

    def _load(self) -> dict[str, _EntryState]:
        if not self._enabled:
            return self._memory
        assert self._path is not None
        try:
            with open(self._path, "r", encoding="utf-8") as handle:
                raw = json.load(handle)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}
        return {key: _EntryState.from_dict(value) for key, value in raw.items()}

    def _save(self, entries: Mapping[str, _EntryState]) -> None:
        if not self._enabled:
            return
        assert self._path is not None
        payload = {key: entry.to_dict() for key, entry in entries.items()}
        directory = os.path.dirname(self._path) or "."
        os.makedirs(directory, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".provider_health_")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle)
            os.replace(tmp_path, self._path)
        except OSError:
            try:
                os.remove(tmp_path)
            except OSError:
                pass

    def _now(self) -> float:
        return float(self._clock())

    def _transition_if_cooldown_elapsed(self, entry: _EntryState) -> _EntryState:
        if entry.state == "open" and self._now() - entry.opened_at >= entry.cooldown_seconds:
            entry.state = "half_open"
        return entry

    def is_open(self, key: str) -> bool:
        """True if ``key`` should be skipped right now (open, not yet cooled down)."""
        if not self._active:
            return False
        if not self._enabled:
            entry = self._memory.get(key)
            if entry is None:
                return False
            entry = self._transition_if_cooldown_elapsed(entry)
            return entry.state == "open"
        with _FileLock(self._path):  # type: ignore[arg-type]
            entries = self._load()
            entry = entries.get(key)
            if entry is None:
                return False
            entry = self._transition_if_cooldown_elapsed(entry)
            entries[key] = entry
            self._save(entries)
            return entry.state == "open"

    def _record(self, key: str, *, success: bool, failure_class: str | None) -> None:
        def apply(entry: _EntryState) -> _EntryState:
            entry.last_updated = self._now()
            if success:
                entry.successes += 1
                entry.consecutive_failures = 0
                entry.consecutive_successes += 1
                if entry.state in {"open", "half_open"}:
                    entry.state = "closed"
                    entry.cooldown_seconds = self._cooldown_seconds
                return entry
            entry.failures += 1
            entry.consecutive_successes = 0
            entry.consecutive_failures += 1
            entry.last_failure_class = failure_class
            if entry.state == "half_open":
                # Probe failed: reopen with a longer cooldown (bounded).
                entry.state = "open"
                entry.opened_at = self._now()
                entry.cooldown_seconds = min(
                    self._max_cooldown_seconds, entry.cooldown_seconds * 2,
                )
            elif entry.consecutive_failures >= self._open_after_failures:
                entry.state = "open"
                entry.opened_at = self._now()
            return entry

        if not self._active:
            return
        if not self._enabled:
            entry = self._memory.get(key, _EntryState(cooldown_seconds=self._cooldown_seconds))
            self._memory[key] = apply(entry)
            return
        with _FileLock(self._path):  # type: ignore[arg-type]
            entries = self._load()
            entry = entries.get(key, _EntryState(cooldown_seconds=self._cooldown_seconds))
            entries[key] = apply(entry)
            self._save(entries)

    def record_success(self, key: str) -> None:
        self._record(key, success=True, failure_class=None)

    def record_failure(self, key: str, failure_class: str) -> None:
        self._record(key, success=False, failure_class=failure_class)

    def snapshot(self) -> dict[str, dict[str, object]]:
        if not self._enabled:
            return {key: entry.to_dict() for key, entry in self._memory.items()}
        with _FileLock(self._path):  # type: ignore[arg-type]
            entries = self._load()
        return {key: entry.to_dict() for key, entry in entries.items()}

    def reset(self) -> None:
        if not self._enabled:
            self._memory.clear()
            return
        try:
            if self._path and os.path.exists(self._path):
                os.remove(self._path)
        except OSError:
            pass
