"""Stage 15 observability layer: logging, correlation ids, timing, metrics,
and diagnostics -- built entirely on the stdlib ``logging`` module.

This module knows nothing about Telegram, ``core.*``, or any specific
service; ``bot/jobs.py``, ``bot/handlers.py``, ``bot/telegram_bot.py``, and
``services/product_verifier.py`` depend on it, never the other way around.

Logging setup is centralized here (``configure_logging``) so production
modules stop configuring ``logging.basicConfig`` themselves. Two output
modes are supported -- plain text and single-line JSON -- both stdlib only.

Secret hygiene: every formatter redacts known secret values (e.g. the
Telegram bot token) and generic authorization-like substrings from its
*final* rendered output, including exception tracebacks, before a single
byte leaves the process.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
import hashlib
import hmac
import json
import logging
import re
import secrets as secrets_module
import sys
import threading
import time
import uuid
from typing import Callable, Iterable, Iterator, Mapping, Sequence

from config import AppConfig


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------

_REDACTED = "***REDACTED***"
_SCOPED_ID_KEY = secrets_module.token_bytes(32)

# Defense-in-depth for anything that looks like a credential even if it was
# never registered as a known secret (e.g. an unexpected value logged by
# accident in an exception message).
_AUTH_HEADER_PATTERN = re.compile(
    r"(?i)(\bauthorization\b[\"']?\s*[:=]\s*)(?:bearer\s+)?([^\"'\s,;}]+)"
)
_BEARER_PATTERN = re.compile(r"(?i)(\bbearer\s+)([^\"'\s,;}]+)")
_SECRET_VALUE_PATTERN = re.compile(
    r"(?i)(\b(?:token|secret|password|api[_-]?key)\b[\"']?\s*[:=]\s*)"
    r"(\"[^\"]*\"|'[^']*'|[^\"'\s,;}]+)"
)


def redact_text(text: str, secrets: Iterable[str] = ()) -> str:
    """Replace every known secret value and any authorization-like substring.

    Applied to the fully-rendered log line (message + args + traceback), so
    a secret can never slip out through a formatting path this module didn't
    anticipate.
    """
    result = text
    for secret in secrets:
        if secret:
            result = result.replace(secret, _REDACTED)
    result = _AUTH_HEADER_PATTERN.sub(lambda m: f"{m.group(1)}{_REDACTED}", result)
    result = _BEARER_PATTERN.sub(lambda m: f"{m.group(1)}{_REDACTED}", result)

    def _replace_secret(match: re.Match[str]) -> str:
        value = match.group(2)
        if value.startswith('"'):
            replacement = f'"{_REDACTED}"'
        elif value.startswith("'"):
            replacement = f"'{_REDACTED}'"
        else:
            replacement = _REDACTED
        return f"{match.group(1)}{replacement}"

    return _SECRET_VALUE_PATTERN.sub(_replace_secret, result)


def scoped_id(value: object, *, length: int = 10) -> str:
    """A short, deterministic, non-reversible id for a chat/user identifier.

    Used in place of a raw Telegram chat/user id in logs -- within one
    process the same input maps to the same output (so operators can still
    correlate events), while a process-random HMAC key prevents recovery or
    correlation across restarts.
    """
    digest = hmac.new(
        _SCOPED_ID_KEY, str(value).encode("utf-8"), hashlib.sha256,
    ).hexdigest()
    return digest[:length]


def new_correlation_id() -> str:
    """A short, opaque id for one service-level verification call."""
    return uuid.uuid4().hex[:16]


# ---------------------------------------------------------------------------
# Formatters
# ---------------------------------------------------------------------------

class _RedactingTextFormatter(logging.Formatter):
    def __init__(self, secrets: Sequence[str] = ()) -> None:
        super().__init__("%(asctime)s %(levelname)s %(name)s %(message)s")
        self._secrets = tuple(secrets)

    def format(self, record: logging.LogRecord) -> str:
        return redact_text(super().format(record), self._secrets)


class SecretRedactionFilter(logging.Filter):
    """Reusable first-pass protection for messages and structured fields.

    The formatter remains the final safety boundary because tracebacks are
    rendered only there.  This filter also protects handlers which inspect a
    record's ordinary message/fields before formatting it.
    """

    def __init__(self, secrets: Sequence[str] = ()) -> None:
        super().__init__()
        self._secrets = tuple(secret for secret in secrets if secret)

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact_text(record.getMessage(), self._secrets)
        record.args = ()
        fields = getattr(record, "fields", None)
        if fields:
            record.fields = _redact_value(fields, self._secrets)
        return True


def _redact_value(value: object, secrets: Sequence[str]) -> object:
    """Redact nested structured fields without retaining mutable aliases."""
    if isinstance(value, str):
        return redact_text(value, secrets)
    if isinstance(value, Mapping):
        return {str(key): _redact_value(item, secrets) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_value(item, secrets) for item in value]
    return value


class _RedactingJsonFormatter(logging.Formatter):
    def __init__(self, secrets: Sequence[str] = ()) -> None:
        super().__init__()
        self._secrets = tuple(secrets)

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "timestamp": self.formatTime(record),
            "level": record.levelname,
            "logger": record.name,
            "event": getattr(record, "event", record.getMessage()),
            "message": record.getMessage(),
        }
        fields = getattr(record, "fields", None)
        if fields:
            payload["fields"] = dict(fields)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        text = json.dumps(payload, ensure_ascii=False, default=str)
        return redact_text(text, self._secrets)


def configure_logging(config: AppConfig, *, secrets: Sequence[str] = ()) -> None:
    """The single place production modules configure logging.

    Safe to call more than once (e.g. once with defaults at process start,
    again once secrets are known) -- it always fully replaces the root
    logger's handlers rather than accumulating duplicates.
    """
    handler = logging.StreamHandler(stream=sys.stderr)
    handler.addFilter(SecretRedactionFilter(secrets))
    if config.log_format == "json":
        handler.setFormatter(_RedactingJsonFormatter(secrets))
    else:
        handler.setFormatter(_RedactingTextFormatter(secrets))

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(config.log_level)


def log_event(logger: logging.Logger, level: int, event: str, **fields: object) -> None:
    """Log one structured operational event: a stable ``event`` name plus
    JSON-safe ``fields``. Renders readably in text mode and structurally in
    JSON mode without callers needing to care which mode is active."""
    if fields:
        rendered = event + " " + " ".join(f"{key}={value!r}" for key, value in fields.items())
    else:
        rendered = event
    logger.log(level, rendered, extra={"event": event, "fields": fields})


def log_exception_event(logger: logging.Logger, event: str, **fields: object) -> None:
    """Log an unexpected failure with a server-side, redacted traceback."""
    if fields:
        rendered = event + " " + " ".join(f"{key}={value!r}" for key, value in fields.items())
    else:
        rendered = event
    logger.error(
        rendered,
        exc_info=True,
        extra={"event": event, "fields": fields},
    )


# ---------------------------------------------------------------------------
# Timing
# ---------------------------------------------------------------------------

class Stopwatch:
    """A small monotonic-clock timer. Never used for the Stage 11 freshness
    clock (that stays ``time.time``-based, unchanged) -- this is purely for
    measuring how long an operation took."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._start = clock()
        self.duration_seconds: float = 0.0

    def elapsed(self) -> float:
        return self._clock() - self._start

    def stop(self) -> float:
        self.duration_seconds = self.elapsed()
        return self.duration_seconds


@contextmanager
def measure(clock: Callable[[], float] = time.monotonic) -> Iterator[Stopwatch]:
    stopwatch = Stopwatch(clock)
    try:
        yield stopwatch
    finally:
        stopwatch.stop()


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

KNOWN_COUNTERS: tuple[str, ...] = (
    "verification_requests",
    "verification_success",
    "verification_failure",
    "cache_hits",
    "cache_misses",
    "jobs_queued",
    "jobs_completed",
    "jobs_failed",
    "jobs_cancelled",
)

KNOWN_DURATIONS: tuple[str, ...] = (
    "cache_lookup",
    "workflow",
    "verification",
    "job",
)


class MetricsCollector:
    """A minimal, thread-safe, in-process counters/timings collector.

    No hidden global instance: callers that want service- and job-level
    counters combined into one diagnostics snapshot must construct one and
    inject it into both (see ``bot/service_factory.py``); each of
    ``ProductVerifierService``/``JobManager`` otherwise gets its own private
    instance, so tests never leak counters across each other.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[str, int] = {name: 0 for name in KNOWN_COUNTERS}
        self._durations: dict[str, dict[str, float | int | None]] = {
            name: {"count": 0, "total_seconds": 0.0, "last_seconds": None}
            for name in KNOWN_DURATIONS
        }

    def increment(self, name: str, amount: int = 1) -> None:
        with self._lock:
            self._counters[name] = self._counters.get(name, 0) + amount

    def record_duration(self, name: str, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("duration must be non-negative")
        with self._lock:
            aggregate = self._durations.setdefault(
                name, {"count": 0, "total_seconds": 0.0, "last_seconds": None},
            )
            aggregate["count"] = int(aggregate["count"] or 0) + 1
            aggregate["total_seconds"] = float(aggregate["total_seconds"] or 0.0) + seconds
            aggregate["last_seconds"] = seconds

    def record_verification_duration(self, seconds: float) -> None:
        """Compatibility-friendly convenience wrapper."""
        self.record_duration("verification", seconds)

    def snapshot(self) -> dict[str, object]:
        """An immutable-in-spirit, JSON-safe view: a fresh plain dict every call."""
        with self._lock:
            counters = dict(self._counters)
            raw_durations = {
                name: dict(aggregate) for name, aggregate in self._durations.items()
            }
        durations: dict[str, dict[str, float | int | None]] = {}
        for name, aggregate in raw_durations.items():
            count = int(aggregate["count"] or 0)
            total = float(aggregate["total_seconds"] or 0.0)
            last = aggregate["last_seconds"]
            durations[name] = {
                "count": count,
                "total_seconds": round(total, 6),
                "last_seconds": round(float(last), 6) if last is not None else None,
                "average_seconds": round(total / count, 6) if count else None,
            }
        return {
            "counters": counters,
            "durations": durations,
        }


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class Diagnostics:
    """Framework-independent snapshot for a future Stage 16 healthcheck.
    Never includes a secret, a raw chat/user id, or product result payloads."""

    service_name: str
    uptime_seconds: float
    active_jobs: int
    queued_jobs: int
    repository_available: bool | None
    metrics: Mapping[str, object]
    extra: Mapping[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        payload = {
            "service_name": self.service_name,
            "uptime_seconds": round(self.uptime_seconds, 3),
            "active_jobs": self.active_jobs,
            "queued_jobs": self.queued_jobs,
            "repository_available": self.repository_available,
            "metrics": dict(self.metrics),
        }
        payload.update(self.extra)
        return payload


def collect_diagnostics(
    *,
    service_name: str,
    started_at: float,
    metrics: MetricsCollector,
    active_jobs: int = 0,
    queued_jobs: int = 0,
    repository_available: bool | None = None,
    clock: Callable[[], float] = time.monotonic,
    extra: Mapping[str, object] | None = None,
) -> Diagnostics:
    uptime = max(0.0, clock() - started_at)
    return Diagnostics(
        service_name=service_name,
        uptime_seconds=uptime,
        active_jobs=active_jobs,
        queued_jobs=queued_jobs,
        repository_available=repository_available,
        metrics=metrics.snapshot(),
        extra=dict(extra or {}),
    )
