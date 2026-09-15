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

from services.product_verifier import ProductVerifierService, VerifyProductRequest, VerifyProductResult


logger = logging.getLogger(__name__)

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
    ) -> None:
        if max_concurrent_jobs < 1:
            raise ValueError("max_concurrent_jobs must be at least 1")
        if history_limit < 0:
            raise ValueError("history_limit must be non-negative")
        self._service = service
        self._clock = clock
        self._history_limit = history_limit
        self._semaphore = asyncio.Semaphore(max_concurrent_jobs)
        self._jobs: dict[str, Job] = {}
        self._chat_active: dict[int, list[str]] = {}
        self._chat_terminal_history: dict[int, list[str]] = {}
        self._active_by_identity: dict[tuple, str] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._callbacks: dict[str, OnUpdate] = {}
        self._accepting = True

    def get_job(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def active_jobs_for_chat(self, chat_id: int) -> list[Job]:
        return [
            self._jobs[job_id] for job_id in self._chat_active.get(chat_id, ())
            if job_id in self._jobs
        ]

    async def submit(
        self,
        chat_id: int,
        request: VerifyProductRequest,
        *,
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

        job = Job(id=uuid.uuid4().hex, chat_id=chat_id, request=request, created_at=self._clock())
        self._jobs[job.id] = job
        self._chat_active.setdefault(chat_id, []).append(job.id)
        self._active_by_identity[identity_key] = job.id
        if on_update is not None:
            self._callbacks[job.id] = on_update
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
            cancelled += 1
        return cancelled

    async def _run(self, job: Job, identity_key: tuple) -> None:
        try:
            async with self._semaphore:
                if job.cancel_requested:
                    raise asyncio.CancelledError
                job.state = "running"
                job.started_at = self._clock()
                await self._notify(job)
                try:
                    result = await asyncio.to_thread(self._service.verify, job.request)
                except asyncio.CancelledError:
                    raise
                except Exception as error:  # noqa: BLE001 - a broken service must not crash the manager
                    logger.exception("Unexpected worker failure for job %s", job.id)
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
            logger.exception("Job update callback failed for job %s", job.id)

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
        for job_id, task in pending:
            job = self._jobs.get(job_id)
            if job is not None and job.state == "queued":
                job.cancel_requested = True
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
