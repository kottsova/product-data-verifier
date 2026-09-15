"""Deterministic Stage 13 JobManager tests. No live network, no real Telegram.

Concurrency/cancellation tests use real threading.Event objects to control
exactly when a fake service.verify() (running in its own worker thread via
asyncio.to_thread) returns -- this is the correct, non-flaky way to test
cross-thread timing, as opposed to guessed sleep() calls. Every gate has an
internal safety timeout so a test bug can never hang the suite.
"""

from __future__ import annotations

import asyncio
import threading
import time
import unittest

from bot.jobs import ACTIVE_STATES, Job, JobManager, JobManagerShuttingDownError, TERMINAL_STATES
from services.product_verifier import (
    VerifyProductError,
    VerifyProductRequest,
    VerifyProductResult,
)


def request(**overrides) -> VerifyProductRequest:
    defaults = dict(brand="Acme", model="X100")
    defaults.update(overrides)
    return VerifyProductRequest(**defaults)


def success_result(req: VerifyProductRequest) -> VerifyProductResult:
    return VerifyProductResult(success=True, request=req)


def failure_result(req: VerifyProductRequest, *, kind="workflow_failure", message="net down"):
    return VerifyProductResult(success=False, request=req, error=VerifyProductError(kind=kind, message=message))


class ImmediateService:
    """A fake service whose verify() returns instantly (no real thread wait)."""

    def __init__(self, factory):
        self._factory = factory
        self.calls: list[VerifyProductRequest] = []

    def verify(self, req: VerifyProductRequest) -> VerifyProductResult:
        self.calls.append(req)
        return self._factory(req) if callable(self._factory) else self._factory


class RaisingService:
    def __init__(self, exception: Exception):
        self.exception = exception
        self.calls: list[VerifyProductRequest] = []

    def verify(self, req: VerifyProductRequest) -> VerifyProductResult:
        self.calls.append(req)
        raise self.exception


class GatedService:
    """verify() blocks (in a real worker thread) until released, per (brand, model).

    Each gate carries its own generous internal safety timeout so a test
    that forgets to release it can never hang the suite indefinitely.
    """

    def __init__(self, *, safety_timeout: float = 5.0):
        self._safety_timeout = safety_timeout
        self._started: dict[str, threading.Event] = {}
        self._gates: dict[str, threading.Event] = {}
        self._lock = threading.Lock()
        self.calls: list[VerifyProductRequest] = []

    @staticmethod
    def _key(brand: str, model: str) -> str:
        return f"{brand}|{model}"

    def verify(self, req: VerifyProductRequest) -> VerifyProductResult:
        key = self._key(req.brand, req.model)
        with self._lock:
            self.calls.append(req)
            started = self._started.setdefault(key, threading.Event())
            gate = self._gates.setdefault(key, threading.Event())
        started.set()
        gate.wait(timeout=self._safety_timeout)
        return success_result(req)

    async def wait_started(self, brand: str, model: str, timeout: float = 2.0) -> bool:
        key = self._key(brand, model)
        with self._lock:
            event = self._started.setdefault(key, threading.Event())
        return await asyncio.to_thread(event.wait, timeout)

    def is_started(self, brand: str, model: str) -> bool:
        key = self._key(brand, model)
        with self._lock:
            event = self._started.get(key)
        return bool(event and event.is_set())

    def release(self, brand: str, model: str) -> None:
        key = self._key(brand, model)
        with self._lock:
            event = self._gates.setdefault(key, threading.Event())
        event.set()

    def release_all(self) -> None:
        with self._lock:
            gates = list(self._gates.values())
        for gate in gates:
            gate.set()


class UpdateRecorder:
    """Captures every on_update call in order and signals on terminal state."""

    def __init__(self):
        self.states: list[str] = []
        self.jobs: list[Job] = []
        self.done = asyncio.Event()

    async def __call__(self, job: Job) -> None:
        self.states.append(job.state)
        self.jobs.append(job)
        if job.state in TERMINAL_STATES:
            self.done.set()

    async def wait(self, timeout: float = 2.0) -> None:
        await asyncio.wait_for(self.done.wait(), timeout=timeout)


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_queued_running_completed_lifecycle(self):
        service = ImmediateService(success_result)
        manager = JobManager(service, max_concurrent_jobs=2)
        recorder = UpdateRecorder()
        job, is_duplicate = await manager.submit(1, request(), on_update=recorder)
        self.assertFalse(is_duplicate)
        self.assertEqual(job.state, "queued")
        await recorder.wait()
        self.assertEqual(recorder.states, ["running", "completed"])
        self.assertEqual(job.state, "completed")
        self.assertIsNotNone(job.result)
        self.assertTrue(job.result.success)
        self.assertIsNotNone(job.started_at)
        self.assertIsNotNone(job.finished_at)

    async def test_workflow_failure_result_becomes_failed_job(self):
        service = ImmediateService(lambda req: failure_result(req, message="network down"))
        manager = JobManager(service, max_concurrent_jobs=1)
        recorder = UpdateRecorder()
        job, _ = await manager.submit(1, request(), on_update=recorder)
        await recorder.wait()
        self.assertEqual(job.state, "failed")
        self.assertEqual(job.error, "network down")
        self.assertIsNotNone(job.result)
        self.assertFalse(job.result.success)

    async def test_unexpected_worker_exception_becomes_failed_job(self):
        service = RaisingService(KeyError("boom"))
        manager = JobManager(service, max_concurrent_jobs=1)
        recorder = UpdateRecorder()
        job, _ = await manager.submit(1, request(), on_update=recorder)
        await recorder.wait()
        self.assertEqual(job.state, "failed")
        self.assertIsNone(job.result)
        self.assertIn("KeyError", job.error)

    async def test_handler_submission_returns_quickly_even_if_verify_never_returns(self):
        """submit() must not block on verify() -- it only schedules a task."""
        service = GatedService(safety_timeout=5.0)
        manager = JobManager(service, max_concurrent_jobs=1)
        started = time.monotonic()
        job, is_duplicate = await asyncio.wait_for(
            manager.submit(1, request(), on_update=UpdateRecorder()), timeout=0.5,
        )
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 0.5)
        self.assertFalse(is_duplicate)
        self.assertIn(job.state, ("queued", "running"))
        service.release_all()  # avoid leaving a real thread blocked past this test


class ConcurrencyLimitTests(unittest.IsolatedAsyncioTestCase):
    async def test_second_job_stays_queued_until_a_slot_frees(self):
        service = GatedService()
        manager = JobManager(service, max_concurrent_jobs=1)
        recorder_a, recorder_b = UpdateRecorder(), UpdateRecorder()
        job_a, _ = await manager.submit(1, request(model="A"), on_update=recorder_a)
        job_b, _ = await manager.submit(1, request(model="B"), on_update=recorder_b)

        self.assertTrue(await service.wait_started("Acme", "A"))
        self.assertFalse(service.is_started("Acme", "B"))
        self.assertEqual(manager.get_job(job_b.id).state, "queued")

        service.release("Acme", "A")
        await recorder_a.wait()
        self.assertTrue(await service.wait_started("Acme", "B"))
        self.assertEqual(manager.get_job(job_b.id).state, "running")

        service.release("Acme", "B")
        await recorder_b.wait()
        self.assertEqual(job_a.state, "completed")
        self.assertEqual(job_b.state, "completed")

    async def test_limit_of_two_admits_exactly_two_concurrently(self):
        service = GatedService()
        manager = JobManager(service, max_concurrent_jobs=2)
        recorders = [UpdateRecorder() for _ in range(3)]
        jobs = []
        for index, recorder in enumerate(recorders):
            job, _ = await manager.submit(1, request(model=f"M{index}"), on_update=recorder)
            jobs.append(job)

        self.assertTrue(await service.wait_started("Acme", "M0"))
        self.assertTrue(await service.wait_started("Acme", "M1"))
        self.assertFalse(service.is_started("Acme", "M2"))
        self.assertEqual(manager.get_job(jobs[2].id).state, "queued")

        service.release("Acme", "M0")
        await recorders[0].wait()
        self.assertTrue(await service.wait_started("Acme", "M2"))

        service.release("Acme", "M1")
        service.release("Acme", "M2")
        await recorders[1].wait()
        await recorders[2].wait()


class DuplicateSuppressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_duplicate_active_request_reuses_the_existing_job(self):
        service = GatedService()
        manager = JobManager(service, max_concurrent_jobs=2)
        job1, dup1 = await manager.submit(1, request(), on_update=UpdateRecorder())
        self.assertFalse(dup1)
        await service.wait_started("Acme", "X100")
        job2, dup2 = await manager.submit(1, request(), on_update=UpdateRecorder())
        self.assertTrue(dup2)
        self.assertEqual(job2.id, job1.id)
        self.assertEqual(len(service.calls), 1)
        service.release("Acme", "X100")

    async def test_different_products_are_never_duplicates(self):
        service = GatedService()
        manager = JobManager(service, max_concurrent_jobs=2)
        job1, dup1 = await manager.submit(1, request(model="A"), on_update=UpdateRecorder())
        job2, dup2 = await manager.submit(1, request(model="B"), on_update=UpdateRecorder())
        self.assertFalse(dup1)
        self.assertFalse(dup2)
        self.assertNotEqual(job1.id, job2.id)
        service.release_all()

    async def test_different_chats_are_never_duplicates(self):
        service = GatedService()
        manager = JobManager(service, max_concurrent_jobs=2)
        job1, dup1 = await manager.submit(1, request(), on_update=UpdateRecorder())
        job2, dup2 = await manager.submit(2, request(), on_update=UpdateRecorder())
        self.assertFalse(dup1)
        self.assertFalse(dup2)
        self.assertNotEqual(job1.id, job2.id)
        service.release_all()

    async def test_a_new_request_after_completion_is_not_a_duplicate(self):
        service = ImmediateService(success_result)
        manager = JobManager(service, max_concurrent_jobs=1)
        recorder1 = UpdateRecorder()
        job1, dup1 = await manager.submit(1, request(), on_update=recorder1)
        await recorder1.wait()
        self.assertFalse(dup1)
        recorder2 = UpdateRecorder()
        job2, dup2 = await manager.submit(1, request(), on_update=recorder2)
        await recorder2.wait()
        self.assertFalse(dup2)
        self.assertNotEqual(job1.id, job2.id)
        self.assertEqual(len(service.calls), 2)

    async def test_key_normalizes_whitespace_and_case(self):
        service = GatedService()
        manager = JobManager(service, max_concurrent_jobs=2)
        job1, dup1 = await manager.submit(1, request(brand="Acme", model="X100"), on_update=UpdateRecorder())
        await service.wait_started("Acme", "X100")
        job2, dup2 = await manager.submit(1, request(brand=" acme ", model="x100"), on_update=UpdateRecorder())
        self.assertFalse(dup1)
        self.assertTrue(dup2)
        self.assertEqual(job1.id, job2.id)
        service.release_all()


class StatusTests(unittest.IsolatedAsyncioTestCase):
    async def test_no_active_jobs_for_a_fresh_chat(self):
        manager = JobManager(ImmediateService(success_result))
        self.assertEqual(manager.active_jobs_for_chat(999), [])

    async def test_active_jobs_include_queued_and_running(self):
        service = GatedService()
        manager = JobManager(service, max_concurrent_jobs=1)
        job_a, _ = await manager.submit(1, request(model="A"), on_update=UpdateRecorder())
        job_b, _ = await manager.submit(1, request(model="B"), on_update=UpdateRecorder())
        await service.wait_started("Acme", "A")
        active = manager.active_jobs_for_chat(1)
        self.assertEqual({job.id for job in active}, {job_a.id, job_b.id})
        service.release_all()

    async def test_completed_jobs_are_not_active(self):
        service = ImmediateService(success_result)
        manager = JobManager(service, max_concurrent_jobs=1)
        recorder = UpdateRecorder()
        job, _ = await manager.submit(1, request(), on_update=recorder)
        await recorder.wait()
        self.assertEqual(manager.active_jobs_for_chat(1), [])


class CancellationTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_with_nothing_active_returns_zero(self):
        manager = JobManager(ImmediateService(success_result))
        self.assertEqual(await manager.cancel_chat_jobs(42), 0)

    async def test_cancelling_a_queued_job_finishes_it_quickly_without_running_verify(self):
        service = GatedService()
        manager = JobManager(service, max_concurrent_jobs=1)
        recorder_a, recorder_b = UpdateRecorder(), UpdateRecorder()
        await manager.submit(1, request(model="A"), on_update=recorder_a)  # occupies the only slot
        job_b, _ = await manager.submit(1, request(model="B"), on_update=recorder_b)
        await service.wait_started("Acme", "A")
        self.assertEqual(job_b.state, "queued")

        cancelled_count = await manager.cancel_chat_jobs(1)
        self.assertEqual(cancelled_count, 2)  # both A (running) and B (queued) are active
        await recorder_b.wait()
        self.assertEqual(job_b.state, "cancelled")
        self.assertIsNone(job_b.result)
        # B was cancelled while still queued -- verify() must never have run for it.
        self.assertFalse(any(call.model == "B" for call in service.calls))

        service.release("Acme", "A")
        await recorder_a.wait()

    async def test_cancelling_a_running_job_discards_its_eventual_result(self):
        service = GatedService()
        manager = JobManager(service, max_concurrent_jobs=1)
        recorder = UpdateRecorder()
        job, _ = await manager.submit(1, request(), on_update=recorder)
        await service.wait_started("Acme", "X100")
        self.assertEqual(job.state, "running")

        cancelled_count = await manager.cancel_chat_jobs(1)
        self.assertEqual(cancelled_count, 1)
        self.assertTrue(job.cancel_requested)
        self.assertEqual(job.state, "running")  # verify() has not returned yet
        self.assertFalse(recorder.done.is_set())

        service.release("Acme", "X100")  # the underlying computation finishes successfully...
        await recorder.wait()
        self.assertEqual(job.state, "cancelled")  # ...but the result is discarded
        self.assertIsNone(job.result)
        self.assertEqual(recorder.states, ["running", "cancelled"])
        self.assertNotIn("completed", recorder.states)

    async def test_cancel_only_affects_the_requesting_chat(self):
        service = GatedService()
        manager = JobManager(service, max_concurrent_jobs=2)
        job_other, _ = await manager.submit(2, request(model="Other"), on_update=UpdateRecorder())
        job_mine, _ = await manager.submit(1, request(model="Mine"), on_update=UpdateRecorder())
        await service.wait_started("Acme", "Other")
        await service.wait_started("Acme", "Mine")

        cancelled_count = await manager.cancel_chat_jobs(1)
        self.assertEqual(cancelled_count, 1)
        self.assertTrue(job_mine.cancel_requested)
        self.assertFalse(job_other.cancel_requested)
        service.release_all()


class RetentionTests(unittest.IsolatedAsyncioTestCase):
    async def test_terminal_history_is_bounded_per_chat(self):
        service = ImmediateService(success_result)
        manager = JobManager(service, max_concurrent_jobs=1, history_limit=2)
        job_ids = []
        for index in range(3):
            recorder = UpdateRecorder()
            job, _ = await manager.submit(1, request(model=f"M{index}"), on_update=recorder)
            await recorder.wait()
            job_ids.append(job.id)
        self.assertIsNone(manager.get_job(job_ids[0]))  # evicted
        self.assertIsNotNone(manager.get_job(job_ids[1]))
        self.assertIsNotNone(manager.get_job(job_ids[2]))

    async def test_history_limit_zero_evicts_immediately(self):
        service = ImmediateService(success_result)
        manager = JobManager(service, max_concurrent_jobs=1, history_limit=0)
        recorder = UpdateRecorder()
        job, _ = await manager.submit(1, request(), on_update=recorder)
        await recorder.wait()
        self.assertIsNone(manager.get_job(job.id))


class ShutdownTests(unittest.IsolatedAsyncioTestCase):
    async def test_shutdown_with_no_jobs_returns_immediately(self):
        manager = JobManager(ImmediateService(success_result))
        await manager.shutdown()  # must not raise or hang

    async def test_shutdown_stops_accepting_new_jobs(self):
        manager = JobManager(ImmediateService(success_result))
        await manager.shutdown()
        with self.assertRaises(JobManagerShuttingDownError):
            await manager.submit(1, request())

    async def test_shutdown_cancels_queued_jobs(self):
        service = GatedService()
        manager = JobManager(service, max_concurrent_jobs=1)
        recorder_a, recorder_b = UpdateRecorder(), UpdateRecorder()
        await manager.submit(1, request(model="A"), on_update=recorder_a)
        job_b, _ = await manager.submit(1, request(model="B"), on_update=recorder_b)
        await service.wait_started("Acme", "A")

        await manager.shutdown(timeout=0.3)
        await recorder_b.wait(timeout=1.0)
        self.assertEqual(job_b.state, "cancelled")

        service.release_all()  # let job A's real thread finish and stop lingering

    async def test_shutdown_does_not_hang_on_a_still_running_worker_thread(self):
        service = GatedService(safety_timeout=3.0)
        manager = JobManager(service, max_concurrent_jobs=1)
        await manager.submit(1, request(), on_update=UpdateRecorder())
        await service.wait_started("Acme", "X100")

        started = time.monotonic()
        await manager.shutdown(timeout=0.2)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 1.0)  # bounded by the shutdown timeout, not the 3s worker gate

        service.release_all()  # cleanup: let the still-running thread finish naturally

    async def test_no_dangling_task_for_a_cancelled_queued_job_after_shutdown(self):
        service = GatedService()
        manager = JobManager(service, max_concurrent_jobs=1)
        await manager.submit(1, request(model="A"), on_update=UpdateRecorder())
        job_b, _ = await manager.submit(1, request(model="B"), on_update=UpdateRecorder())
        await service.wait_started("Acme", "A")

        await manager.shutdown(timeout=0.3)
        self.assertNotIn(job_b.id, manager._tasks)

        service.release_all()


class ConfigurationValidationTests(unittest.TestCase):
    def test_zero_or_negative_concurrency_is_rejected(self):
        with self.assertRaises(ValueError):
            JobManager(ImmediateService(success_result), max_concurrent_jobs=0)

    def test_negative_history_limit_is_rejected(self):
        with self.assertRaises(ValueError):
            JobManager(ImmediateService(success_result), max_concurrent_jobs=1, history_limit=-1)


if __name__ == "__main__":
    unittest.main()
