"""Stage 13 in-memory background job lifecycle for Telegram verification.

A Job wraps one VerifyProductRequest / eventual VerifyProductResult -- both
are the stable services.product_verifier DTOs, never a core.* pipeline type.
JobManager only depends on ProductVerifierService; it knows nothing about
Telegram, python-telegram-bot, or core.workflow/profile/quality.

Persistence note: jobs live only for the process lifetime. This is a
deliberate MVP scope boundary (see the Stage 13 report/README) -- the
Stage 11 product cache is persistent (SQLite), this job state is not. A
process restart loses in-flight job tracking; a user can simply resend
their message, and a completed product's Stage 11 cache entry (if any)
still avoids redoing the live pipeline.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
import time
import unicodedata
import uuid
from typing import Awaitable, Callable, Literal

from bot.i18n import DEFAULT_LANGUAGE, Language
from observability import (
    Diagnostics,
    MetricsCollector,
    Stopwatch,
    collect_diagnostics,
    log_event,
    log_exception_event,
    scoped_id,
)
from services.product_verifier import ProductVerifierService, VerifyProductRequest, VerifyProductResult


logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

JobState = Literal["queued", "running", "completed", "failed", "cancelled"]
JOB_STATES = {"queued", "running", "completed", "failed", "cancelled"}
ACTIVE_STATES = {"queued", "running"}
TERMINAL_STATES = {"completed", "failed", "cancelled"}


@dataclass(slots=True)
class Job:
    """The public job contract. Deliberately not a core.* DTO."""

    id: str
    chat_id: int
    request: VerifyProductRequest
    language: Language = DEFAULT_LANGUAGE
    state: JobState = "queued"
    created_at: float = 0.0
    started_at: float | None = None
    finished_at: float | None = None
    result: VerifyProductResult | None = None
    error: str | None = None
    cancel_requested: bool = False


OnUpdate = Callable[[Job], Awaitable[None]]


class JobManagerShuttingDownError(RuntimeError):
    """Raised by submit() once shutdown() has begun; no new jobs are accepted."""


def _normalize(value: object) -> str:
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).split()).casefold()


def _product_label(request: VerifyProductRequest) -> str:
    """Brand/model is product data, not personal data -- safe to log (Stage 15)."""
    return f"{request.brand} {request.model}".strip()


def _request_identity(request: VerifyProductRequest) -> tuple[object, ...]:
    """A bot-layer-local canonical identity, for duplicate-request detection.

    Conceptually mirrors services.product_verifier's private cache key (same
    normalized fields), but is implemented independently here -- the bot
    layer must not depend on that module's private implementation.
    """
    return (
        _normalize(request.brand), _normalize(request.model), _normalize(request.article),
        _normalize(request.market), request.max_sources, request.targeted_search_enabled,
    )


class JobManager:
    """Bounded-concurrency background execution of ProductVerifierService.verify().

    Not a Celery/Redis-style queue: an asyncio.Semaphore caps how many
    verify() calls run at once (each in its own worker thread via
    asyncio.to_thread), and extra submissions simply wait in the "queued"
    state until a slot frees up.
    """

    def __init__(
        self,
        service: ProductVerifierService,
        *,
        max_concurrent_jobs: int = 2,
        history_limit: int = 20,
        clock: Callable[[], float] = time.time,
        duration_clock: Callable[[], float] = time.monotonic,
        metrics: MetricsCollector | None = None,
    ) -> None:
        if max_concurrent_jobs < 1:
            raise ValueError("max_concurrent_jobs must be at least 1")
        if history_limit < 0:
            raise ValueError("history_limit must be non-negative")
        self._service = service
        self._clock = clock
        self._duration_clock = duration_clock
        self._history_limit = history_limit
        self._semaphore = asyncio.Semaphore(max_concurrent_jobs)
        self._jobs: dict[str, Job] = {}
        self._pending_queries: dict[int, VerifyProductRequest] = {}
        self._chat_active: dict[int, list[str]] = {}
        self._chat_terminal_history: dict[int, list[str]] = {}
        self._chat_last_success: dict[int, Job] = {}
        self._active_by_identity: dict[tuple, str] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._callbacks: dict[str, OnUpdate] = {}
        self._stopwatches: dict[str, Stopwatch] = {}
        self._accepting = True
        self._metrics = metrics if metrics is not None else MetricsCollector()
        self._started_at = self._duration_clock()

    @property
    def metrics(self) -> MetricsCollector:
        return self._metrics

    def get_job(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def set_pending_query(self, chat_id: int, request: VerifyProductRequest) -> None:
        """Stage 31.1: a parsed query awaiting the chat's RU/EN language pick.

        One pending query per chat -- a second product message before the
        first is answered simply replaces it (last message wins, mirroring
        how a resent /verify-style message already behaves elsewhere).
        """
        self._pending_queries[chat_id] = request

    def pop_pending_query(self, chat_id: int) -> VerifyProductRequest | None:
        return self._pending_queries.pop(chat_id, None)

    def active_jobs_for_chat(self, chat_id: int) -> list[Job]:
        return [
            self._jobs[job_id] for job_id in self._chat_active.get(chat_id, ())
            if job_id in self._jobs
        ]

    def last_export_job_for_chat(self, chat_id: int) -> Job | None:
        """The most recent successfully completed job for a chat, for /export.

        Tracked independently of _chat_terminal_history's bounded eviction so
        a burst of later failed/cancelled jobs never hides a still-usable
        result (Stage 30: a failed result must not overwrite the last usable
        export).
        """
        return self._chat_last_success.get(chat_id)

    def active_job_count(self) -> int:
        """Stage 15 diagnostics: global active (queued+running) job count."""
        return sum(1 for job in self._jobs.values() if job.state in ACTIVE_STATES)

    def queued_job_count(self) -> int:
        """Stage 15 diagnostics: global queued-only job count."""
        return sum(1 for job in self._jobs.values() if job.state == "queued")

    def diagnostics(self) -> Diagnostics:
        """Internal, framework-independent health/operations snapshot."""
        repository_available = None
        repository_configured = False
        probe = getattr(self._service, "repository_available", None)
        if callable(probe):
            repository_available = probe()
            repository_configured = bool(
                getattr(self._service, "repository_configured", repository_available is not None)
            )
        cache_schema_version = getattr(self._service, "cache_schema_version", None)
        versions = (
            {"cache_schema": cache_schema_version}
            if cache_schema_version is not None else {}
        )
        return collect_diagnostics(
            service_name="product-data-verifier",
            started_at=self._started_at,
            metrics=self._metrics,
            active_jobs=self.active_job_count(),
            queued_jobs=self.queued_job_count(),
            repository_available=repository_available,
            clock=self._duration_clock,
            extra={
                "repository": {
                    "configured": repository_configured,
                    "available": repository_available,
                },
                "versions": versions,
            },
        )

    async def submit(
        self,
        chat_id: int,
        request: VerifyProductRequest,
        *,
        language: Language = DEFAULT_LANGUAGE,
        on_update: OnUpdate | None = None,
    ) -> tuple[Job, bool]:
        """Create (or reuse) a job for this chat/request. Returns (job, is_duplicate)."""
        if not self._accepting:
            raise JobManagerShuttingDownError("job manager is shutting down; not accepting new jobs")

        identity_key = (chat_id, _request_identity(request))
        existing_id = self._active_by_identity.get(identity_key)
        if existing_id is not None:
            existing = self._jobs.get(existing_id)
            if existing is not None and existing.state in ACTIVE_STATES:
                return existing, True

        job = Job(id=uuid.uuid4().hex, chat_id=chat_id, request=request, language=language, created_at=self._clock())
        self._jobs[job.id] = job
        self._chat_active.setdefault(chat_id, []).append(job.id)
        self._active_by_identity[identity_key] = job.id
        self._stopwatches[job.id] = Stopwatch(self._duration_clock)
        if on_update is not None:
            self._callbacks[job.id] = on_update
        self._metrics.increment("jobs_queued")
        log_event(
            logger, logging.INFO, "job_queued",
            job_id=job.id, chat=scoped_id(chat_id), product=_product_label(request),
        )
        task = asyncio.create_task(self._run(job, identity_key))
        self._tasks[job.id] = task
        return job, False

    async def cancel_chat_jobs(self, chat_id: int) -> int:
        """Cooperatively cancel every active job for a chat. Returns how many.

        A queued job's task is cancelled outright (it is only waiting on the
        concurrency semaphore, a safely cancellable await). A running job is
        only flagged: its blocking verify() call cannot be force-killed, so
        it may finish in the background, but _run() discards the result
        instead of delivering it once cancel_requested is set.
        """
        cancelled = 0
        for job_id in list(self._chat_active.get(chat_id, ())):
            job = self._jobs.get(job_id)
            if job is None or job.state not in ACTIVE_STATES:
                continue
            job.cancel_requested = True
            if job.state == "queued":
                task = self._tasks.get(job_id)
                if task is not None:
                    task.cancel()
                    # A task cancelled before its coroutine gets its first
                    # timeslice never enters _run()'s CancelledError handler.
                    # Give an entered task one turn to finalize itself, then
                    # finalize the never-started case here.
                    await asyncio.sleep(0)
                    if job.state in ACTIVE_STATES:
                        await self._finalize(
                            job, (chat_id, _request_identity(job.request)), "cancelled",
                        )
            cancelled += 1
        return cancelled

    async def _run(self, job: Job, identity_key: tuple) -> None:
        try:
            async with self._semaphore:
                if job.cancel_requested:
                    raise asyncio.CancelledError
                job.state = "running"
                job.started_at = self._clock()
                log_event(
                    logger, logging.INFO, "job_started",
                    job_id=job.id, chat=scoped_id(job.chat_id), product=_product_label(job.request),
                )
                await self._notify(job)
                try:
                    if isinstance(self._service, ProductVerifierService):
                        result = await asyncio.to_thread(
                            self._service.verify, job.request, correlation_id=job.id,
                        )
                    else:
                        # Stage 13's intentionally tiny duck-typed test/services
                        # retain their original one-argument public shape.
                        result = await asyncio.to_thread(self._service.verify, job.request)
                except asyncio.CancelledError:
                    raise
                except Exception as error:  # noqa: BLE001 - a broken service must not crash the manager
                    log_exception_event(logger, "job_worker_exception", job_id=job.id)
                    await self._finalize(
                        job, identity_key, "failed", error=f"{type(error).__name__}: {error}",
                    )
                    return
                if job.cancel_requested:
                    raise asyncio.CancelledError
                if result.success:
                    await self._finalize(job, identity_key, "completed", result=result)
                else:
                    message = result.error.message if result.error else "unknown error"
                    await self._finalize(job, identity_key, "failed", result=result, error=message)
        except asyncio.CancelledError:
            await self._finalize(job, identity_key, "cancelled")

    async def _finalize(
        self,
        job: Job,
        identity_key: tuple,
        state: JobState,
        *,
        result: VerifyProductResult | None = None,
        error: str | None = None,
    ) -> None:
        job.state = state
        job.finished_at = self._clock()
        job.result = result
        job.error = error
        self._tasks.pop(job.id, None)
        chat_active = self._chat_active.get(job.chat_id)
        if chat_active and job.id in chat_active:
            chat_active.remove(job.id)
        if self._active_by_identity.get(identity_key) == job.id:
            del self._active_by_identity[identity_key]

        stopwatch = self._stopwatches.pop(job.id, None)
        duration = stopwatch.stop() if stopwatch is not None else None
        fields: dict[str, object] = {
            "job_id": job.id, "chat": scoped_id(job.chat_id), "product": _product_label(job.request),
        }
        if duration is not None:
            self._metrics.record_duration("job", duration)
            fields["duration_seconds"] = round(duration, 6)
        if state == "completed":
            self._metrics.increment("jobs_completed")
            log_event(logger, logging.INFO, "job_completed", **fields)
            if result is not None and result.success:
                self._chat_last_success[job.chat_id] = job
        elif state == "failed":
            self._metrics.increment("jobs_failed")
            log_event(logger, logging.WARNING, "job_failed", had_error=bool(error), **fields)
        elif state == "cancelled":
            self._metrics.increment("jobs_cancelled")
            log_event(logger, logging.INFO, "job_cancelled", **fields)

        await self._notify(job)
        self._callbacks.pop(job.id, None)
        self._record_terminal_history(job)

    async def _notify(self, job: Job) -> None:
        callback = self._callbacks.get(job.id)
        if callback is None:
            return
        try:
            await callback(job)
        except Exception:  # noqa: BLE001 - a notification failure must not break the manager
            log_exception_event(logger, "job_notification_failure", job_id=job.id)

    def _record_terminal_history(self, job: Job) -> None:
        """Bounded retention: this is job state hygiene, not the Stage 11 cache."""
        if self._history_limit == 0:
            self._jobs.pop(job.id, None)
            return
        history = self._chat_terminal_history.setdefault(job.chat_id, [])
        history.append(job.id)
        while len(history) > self._history_limit:
            evicted_id = history.pop(0)
            self._jobs.pop(evicted_id, None)

    async def shutdown(self, *, timeout: float | None = None) -> None:
        """Stop accepting new jobs; cancel queued ones; wait (bounded) for the rest.

        A running job's worker thread cannot be force-killed (see
        cancel_chat_jobs); if it is still going when `timeout` elapses, this
        returns anyway rather than hanging forever, logging that the thread
        may still finish in the background (its result is simply never
        delivered, since finalize() only calls a callback that is no longer
        useful after process shutdown proceeds).
        """
        self._accepting = False
        pending = list(self._tasks.items())
        queued_before_cancel: list[tuple[Job, tuple[object, ...]]] = []
        for job_id, task in pending:
            job = self._jobs.get(job_id)
            if job is not None and job.state == "queued":
                job.cancel_requested = True
                queued_before_cancel.append(
                    (job, (job.chat_id, _request_identity(job.request))),
                )
                task.cancel()
        if not pending:
            return
        waiter = asyncio.gather(*(task for _, task in pending), return_exceptions=True)
        try:
            await asyncio.wait_for(waiter, timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning(
                "Shutdown timed out waiting for %d job(s); still-running worker "
                "threads were not force-killed and may finish in the background.",
                len(pending),
            )
        finally:
            # See cancel_chat_jobs(): a coroutine cancelled before its first
            # timeslice cannot run its own CancelledError finalizer.
            for job, identity_key in queued_before_cancel:
                if job.state in ACTIVE_STATES:
                    await self._finalize(job, identity_key, "cancelled")
