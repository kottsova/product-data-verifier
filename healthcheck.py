"""Machine-readable deployment health probe for Product Data Verifier.

The probe intentionally does not load TelegramConfig, contact Telegram, or
run a product verification. It validates the centralized non-secret config,
opens the configured SQLite repository, and exercises the Stage 15 diagnostics
snapshot used by a future deployment integration.
"""

from __future__ import annotations

import json
import time
from typing import Callable, Mapping

from bot.jobs import JobManager
from config import AppConfig, ConfigurationError
from observability import MetricsCollector
from services.cache import CachePolicy, SqliteProductVerificationRepository
from services.product_verifier import ProductVerifierService


HEALTHY = "healthy"
UNHEALTHY = "unhealthy"


def _failure(checks: Mapping[str, bool], code: str, error: Exception) -> dict[str, object]:
    """Return a stable failure payload without paths, messages, or secrets."""
    return {
        "status": UNHEALTHY,
        "checks": dict(checks),
        "error": code,
        "error_type": type(error).__name__,
    }


def build_health_snapshot(
    env: Mapping[str, str] | None = None,
    *,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, object]:
    """Build a JSON-safe health snapshot with no network or destructive I/O."""
    checks = {"config": False, "repository": False, "diagnostics": False}
    try:
        config = AppConfig.from_env(env=env)
        checks["config"] = True
    except ConfigurationError as error:
        return _failure(checks, "invalid_configuration", error)

    try:
        repository = SqliteProductVerificationRepository(config.db_path)
        if not repository.is_available():
            raise RuntimeError("repository health probe failed")
        checks["repository"] = True
    except Exception as error:  # noqa: BLE001 - health boundary must return JSON, never traceback
        return _failure(checks, "repository_unavailable", error)

    try:
        metrics = MetricsCollector()
        service = ProductVerifierService(
            repository=repository,
            cache_policy=CachePolicy(ttl_seconds=config.cache_ttl_seconds),
            metrics=metrics,
        )
        manager = JobManager(
            service,
            max_concurrent_jobs=config.max_concurrent_jobs,
            history_limit=config.job_history_limit,
            duration_clock=clock,
            metrics=metrics,
        )
        diagnostics = manager.diagnostics().to_dict()
        checks["diagnostics"] = True
    except Exception as error:  # noqa: BLE001 - health boundary must return JSON, never traceback
        return _failure(checks, "diagnostics_unavailable", error)

    return {
        "status": HEALTHY,
        "checks": checks,
        "diagnostics": diagnostics,
    }


def main(*, env: Mapping[str, str] | None = None) -> int:
    snapshot = build_health_snapshot(env=env)
    print(json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0 if snapshot["status"] == HEALTHY else 1


if __name__ == "__main__":
    raise SystemExit(main())
