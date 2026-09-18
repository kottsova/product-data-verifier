"""Deterministic Stage 15 observability/operations tests (no network)."""

from __future__ import annotations

import asyncio
import io
import json
import logging
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from bot.handlers import (
    _start_verification,
    build_cancel_command,
    build_status_command,
    handle_product_query,
    start_command,
)
from bot.jobs import JobManager
from bot.telegram_bot import build_application, main as bot_main
from config import AppConfig, ConfigurationError, TelegramConfig
from observability import (
    MetricsCollector,
    configure_logging,
    log_exception_event,
    redact_text,
)
from services.cache import (
    CacheEntry,
    CachePolicy,
    InMemoryProductVerificationRepository,
    SqliteProductVerificationRepository,
)
from services.product_verifier import (
    ProductVerifierService,
    VerifyProductError,
    VerifyProductRequest,
    VerifyProductResult,
)
from tests.test_product_verifier import fake_runner
from tests.test_profile_export import candidate, definition, final_profile


class _EventHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    def events(self, name: str) -> list[logging.LogRecord]:
        return [record for record in self.records if getattr(record, "event", None) == name]


class _LoggingIsolationMixin:
    def setUp(self) -> None:
        super().setUp()
        root = logging.getLogger()
        self._old_handlers = list(root.handlers)
        self._old_level = root.level
        for handler in list(root.handlers):
            root.removeHandler(handler)
        root.setLevel(logging.DEBUG)
        self.events = _EventHandler()
        root.addHandler(self.events)

    def tearDown(self) -> None:
        root = logging.getLogger()
        for handler in list(root.handlers):
            root.removeHandler(handler)
        for handler in self._old_handlers:
            root.addHandler(handler)
        root.setLevel(self._old_level)
        super().tearDown()


class _StepClock:
    def __init__(self, start: float = 0.0, step: float = 1.0) -> None:
        self.value = start
        self.step = step

    def __call__(self) -> float:
        current = self.value
        self.value += self.step
        return current


def _profile():
    return final_profile([definition("power")], [candidate("power", "1000", unit="W")])


class LoggingConfigurationTests(_LoggingIsolationMixin, unittest.TestCase):
    def test_valid_and_invalid_log_levels(self):
        self.assertEqual(
            AppConfig.from_env(env={"PRODUCT_VERIFIER_LOG_LEVEL": "debug"}).log_level,
            "DEBUG",
        )
        with self.assertRaises(ConfigurationError):
            AppConfig.from_env(env={"PRODUCT_VERIFIER_LOG_LEVEL": "verbose"})

    def test_logging_configuration_replaces_handlers_and_honours_level(self):
        stream = io.StringIO()
        old_stderr = __import__("sys").stderr
        try:
            __import__("sys").stderr = stream
            configure_logging(AppConfig(log_level="ERROR"))
            logger = logging.getLogger("stage15.config")
            logger.info("hidden")
            logger.error("visible")
        finally:
            __import__("sys").stderr = old_stderr
        root = logging.getLogger()
        self.assertEqual(root.level, logging.ERROR)
        self.assertEqual(len(root.handlers), 1)
        self.assertNotIn("hidden", stream.getvalue())
        self.assertIn("visible", stream.getvalue())

    def test_secret_redaction_covers_plain_exception_and_config_logging(self):
        token = "123456:ABC-super-secret"
        stream = io.StringIO()
        old_stderr = __import__("sys").stderr
        try:
            __import__("sys").stderr = stream
            configure_logging(AppConfig(), secrets=(token,))
            logger = logging.getLogger("stage15.secrets")
            logger.error("ordinary token=%s", token)
            try:
                raise RuntimeError(f"failed with {token}")
            except RuntimeError:
                log_exception_event(logger, "secret_exception")
            logger.error("config=%r", TelegramConfig(bot_token=token))
        finally:
            __import__("sys").stderr = old_stderr
        output = stream.getvalue()
        self.assertNotIn(token, output)
        self.assertIn("***REDACTED***", output)
        self.assertIn("Traceback", output)

    def test_authorization_like_strings_are_redacted(self):
        self.assertNotIn("abc123", redact_text("Authorization: Bearer abc123"))
        self.assertNotIn("abc123", redact_text("Bearer abc123"))
        self.assertNotIn("abc123", redact_text("api_key=abc123"))

    def test_json_logging_is_single_line_json_and_redacted(self):
        token = "123:json-secret"
        stream = io.StringIO()
        old_stderr = __import__("sys").stderr
        try:
            __import__("sys").stderr = stream
            configure_logging(AppConfig(log_format="json"), secrets=(token,))
            logging.getLogger("stage15.json").error("token=%s", token)
        finally:
            __import__("sys").stderr = old_stderr
        payload = json.loads(stream.getvalue())
        self.assertEqual(payload["level"], "ERROR")
        self.assertNotIn(token, stream.getvalue())


class ServiceEventTests(_LoggingIsolationMixin, unittest.TestCase):
    def test_start_completed_correlation_and_workflow_total_timings(self):
        metrics = MetricsCollector()
        service = ProductVerifierService(
            run_workflow=fake_runner(_profile()), metrics=metrics, duration_clock=_StepClock(),
        )
        result = service.verify(
            VerifyProductRequest(brand="Acme", model="X100"), correlation_id="request-1",
        )
        self.assertTrue(result.success)
        started = self.events.events("verification_started")[0]
        completed = self.events.events("verification_completed")[0]
        self.assertEqual(started.fields["correlation_id"], "request-1")
        self.assertEqual(completed.fields["correlation_id"], "request-1")
        self.assertGreater(completed.fields["workflow_duration_seconds"], 0)
        self.assertGreater(completed.fields["duration_seconds"], 0)
        snapshot = metrics.snapshot()
        self.assertEqual(snapshot["durations"]["workflow"]["count"], 1)
        self.assertEqual(snapshot["durations"]["verification"]["count"], 1)

    def test_expected_failure_has_no_traceback_and_unexpected_has_one(self):
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        logging.getLogger().addHandler(handler)
        expected = ProductVerifierService(run_workflow=lambda _request: (_ for _ in ()).throw(RuntimeError("down")))
        expected.verify(VerifyProductRequest(brand="Acme", model="X100"))
        self.assertIn("verification_failed", stream.getvalue())
        self.assertNotIn("Traceback", stream.getvalue())

        stream.seek(0)
        stream.truncate(0)
        unexpected = ProductVerifierService(run_workflow=lambda _request: (_ for _ in ()).throw(KeyError("boom")))
        unexpected.verify(VerifyProductRequest(brand="Acme", model="X100"))
        self.assertIn("verification_exception", stream.getvalue())
        self.assertIn("Traceback", stream.getvalue())

    def test_verification_failure_event_and_counters(self):
        metrics = MetricsCollector()
        service = ProductVerifierService(
            run_workflow=lambda _request: (_ for _ in ()).throw(RuntimeError("down")),
            metrics=metrics,
        )
        result = service.verify(VerifyProductRequest(brand="Acme", model="X100"))
        self.assertFalse(result.success)
        self.assertEqual(len(self.events.events("verification_failed")), 1)
        counters = metrics.snapshot()["counters"]
        self.assertEqual(counters["verification_requests"], 1)
        self.assertEqual(counters["verification_failure"], 1)


class CacheEventTests(_LoggingIsolationMixin, unittest.TestCase):
    def _service(self, repository, *, freshness_clock=lambda: 100.0, duration_clock=None):
        return ProductVerifierService(
            run_workflow=fake_runner(_profile()),
            repository=repository,
            cache_policy=CachePolicy(ttl_seconds=10.0),
            clock=freshness_clock,
            duration_clock=duration_clock or _StepClock(),
        )

    def test_cache_miss_hit_and_lookup_duration(self):
        repository = InMemoryProductVerificationRepository()
        service = self._service(repository)
        request = VerifyProductRequest(brand="Acme", model="X100")
        service.verify(request, correlation_id="miss")
        service.verify(request, correlation_id="hit")
        self.assertEqual(len(self.events.events("cache_miss")), 1)
        self.assertEqual(len(self.events.events("cache_hit")), 1)
        self.assertEqual(service.metrics.snapshot()["counters"]["cache_hits"], 1)
        self.assertEqual(service.metrics.snapshot()["counters"]["cache_misses"], 1)
        self.assertEqual(service.metrics.snapshot()["durations"]["cache_lookup"]["count"], 2)

    def test_cache_stale_event(self):
        repository = InMemoryProductVerificationRepository()
        service = self._service(repository, freshness_clock=lambda: 100.0)
        request = VerifyProductRequest(brand="Acme", model="X100")
        service.verify(request)
        service._clock = lambda: 111.0
        service.verify(request)
        self.assertEqual(len(self.events.events("cache_stale")), 1)

    def test_cache_read_and_write_failures(self):
        class BrokenRepository:
            def get(self, key):
                raise OSError("read failed")

            def save(self, entry):
                raise OSError("write failed")

        service = self._service(BrokenRepository())
        result = service.verify(VerifyProductRequest(brand="Acme", model="X100"))
        self.assertTrue(result.success)
        self.assertEqual(len(self.events.events("cache_read_failure")), 1)
        self.assertEqual(len(self.events.events("cache_write_failure")), 1)


class JobEventTests(_LoggingIsolationMixin, unittest.IsolatedAsyncioTestCase):
    async def test_job_events_duration_metrics_and_service_correlation(self):
        metrics = MetricsCollector()
        service = ProductVerifierService(run_workflow=fake_runner(_profile()), metrics=metrics)
        manager = JobManager(service, metrics=metrics, duration_clock=_StepClock())
        job, duplicate = await manager.submit(
            42, VerifyProductRequest(brand="Acme", model="X100"),
        )
        self.assertFalse(duplicate)
        await manager._tasks[job.id]
        self.assertEqual(job.state, "completed")
        for event in ("job_queued", "job_started", "job_completed"):
            self.assertEqual(len(self.events.events(event)), 1)
        verification = self.events.events("verification_started")[0]
        self.assertEqual(verification.fields["correlation_id"], job.id)
        snapshot = metrics.snapshot()
        self.assertEqual(snapshot["counters"]["jobs_queued"], 1)
        self.assertEqual(snapshot["counters"]["jobs_completed"], 1)
        self.assertEqual(snapshot["durations"]["job"]["count"], 1)

    async def test_job_failed_and_cancelled_events(self):
        failure = VerifyProductResult(
            request=VerifyProductRequest(brand="Acme", model="bad"),
            success=False,
            error=VerifyProductError(kind="workflow_failure", message="down"),
        )

        class FailedService:
            def verify(self, request):
                return failure

        failed_manager = JobManager(FailedService())
        failed, _ = await failed_manager.submit(1, failure.request)
        await failed_manager._tasks[failed.id]
        self.assertEqual(failed.state, "failed")
        self.assertEqual(len(self.events.events("job_failed")), 1)

        class NeverRunService:
            def verify(self, request):
                raise AssertionError("cancelled queued job must not run")

        cancelled_manager = JobManager(NeverRunService())
        cancelled, _ = await cancelled_manager.submit(
            2, VerifyProductRequest(brand="Acme", model="cancel"),
        )
        cancelled_task = cancelled_manager._tasks[cancelled.id]
        await cancelled_manager.cancel_chat_jobs(2)
        await asyncio.gather(cancelled_task, return_exceptions=True)
        self.assertEqual(cancelled.state, "cancelled")
        self.assertEqual(len(self.events.events("job_cancelled")), 1)


class MetricsDiagnosticsTests(_LoggingIsolationMixin, unittest.TestCase):
    def test_metrics_aggregates_are_fresh_json_safe_snapshots(self):
        metrics = MetricsCollector()
        metrics.increment("verification_requests")
        metrics.record_duration("verification", 1.25)
        metrics.record_duration("verification", 2.75)
        first = metrics.snapshot()
        self.assertEqual(first["durations"]["verification"], {
            "count": 2,
            "total_seconds": 4.0,
            "last_seconds": 2.75,
            "average_seconds": 2.0,
        })
        json.dumps(first)
        first["counters"]["verification_requests"] = 999
        self.assertEqual(metrics.snapshot()["counters"]["verification_requests"], 1)

    def test_diagnostics_snapshot_uptime_jobs_repository_and_version(self):
        clock = _StepClock(start=10.0, step=5.0)
        service = ProductVerifierService(run_workflow=fake_runner(_profile()))
        manager = JobManager(service, duration_clock=clock, metrics=service.metrics)
        snapshot = manager.diagnostics().to_dict()
        self.assertEqual(snapshot["uptime_seconds"], 5.0)
        self.assertEqual(snapshot["active_jobs"], 0)
        self.assertEqual(snapshot["queued_jobs"], 0)
        self.assertEqual(snapshot["repository"], {"configured": False, "available": None})
        self.assertEqual(snapshot["versions"]["cache_schema"], 1)
        json.dumps(snapshot)

    def test_sqlite_health_probe_is_read_only_and_reports_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "health.sqlite3"
            repository = SqliteProductVerificationRepository(path)
            with repository._connection() as connection:
                before = connection.execute("SELECT COUNT(*) FROM verification_cache").fetchone()[0]
            self.assertTrue(repository.is_available())
            with repository._connection() as connection:
                after = connection.execute("SELECT COUNT(*) FROM verification_cache").fetchone()[0]
            self.assertEqual(before, after)

        broken = object.__new__(SqliteProductVerificationRepository)
        broken._path = "\x00invalid"
        self.assertFalse(broken.is_available())
        self.assertEqual(len(self.events.events("repository_health_failure")), 1)


class TelegramOperationalEventTests(_LoggingIsolationMixin, unittest.IsolatedAsyncioTestCase):
    async def test_request_duplicate_status_cancel_and_private_fields(self):
        class SlowService:
            def verify(self, request):
                return VerifyProductResult(request=request, success=True)

        manager = JobManager(SlowService())
        replies: list[str] = []

        async def reply(text: str):
            replies.append(text)

        async def prompt_language(text: str, buttons):
            return None

        async def ask_and_start(chat_id: int) -> None:
            await handle_product_query(
                "Acme X100", chat_id, manager, reply=reply, prompt_language=prompt_language,
            )
            request = manager.pop_pending_query(chat_id)
            await _start_verification(request, chat_id, manager, language="ru", reply=reply)

        raw_chat = 987654321012345
        await ask_and_start(raw_chat)
        await ask_and_start(raw_chat)
        self.assertEqual(len(self.events.events("request_accepted")), 1)
        self.assertEqual(len(self.events.events("duplicate_request")), 1)

        class Message:
            chat_id = raw_chat
            username = "private_username"
            first_name = "Private"
            sent: list[str] = []

            async def reply_text(self, text):
                self.sent.append(text)

        class Update:
            message = Message()
            secret_profile_dump = "must-not-appear"

        await build_status_command(manager)(Update(), None)
        self.assertEqual(len(self.events.events("status_action")), 1)
        await build_cancel_command(manager)(Update(), None)
        self.assertEqual(len(self.events.events("cancel_action")), 1)
        rendered = "\n".join(record.getMessage() for record in self.events.records)
        self.assertNotIn(str(raw_chat), rendered)
        self.assertNotIn("private_username", rendered)
        self.assertNotIn("must-not-appear", rendered)
        await manager.shutdown(timeout=1)

    async def test_bot_startup_and_shutdown_events(self):
        service = ProductVerifierService(run_workflow=fake_runner(_profile()))
        manager = JobManager(service)
        application = build_application("123:abc", manager)
        await application.post_init(application)
        await application.post_shutdown(application)
        self.assertEqual(len(self.events.events("bot_started")), 1)
        self.assertEqual(len(self.events.events("bot_shutdown")), 1)

    async def test_reply_failure_is_an_operational_event_with_traceback(self):
        class BrokenMessage:
            chat_id = 123456789

            async def reply_text(self, text):
                raise ConnectionError("Telegram API unavailable")

        class Update:
            message = BrokenMessage()

        await start_command(Update(), None)
        failures = self.events.events("reply_send_failure")
        self.assertEqual(len(failures), 1)
        self.assertNotIn(str(Update.message.chat_id), failures[0].getMessage())
        self.assertIsNotNone(failures[0].exc_info)


class TelegramMainEventTests(unittest.TestCase):
    def test_main_emits_bot_starting_without_logging_token(self):
        config = AppConfig(db_path="unused.sqlite3")
        telegram = TelegramConfig(bot_token="123:main-secret")

        class Application:
            def run_polling(self, **kwargs):
                return None

        with (
            patch("bot.telegram_bot.AppConfig.from_env", return_value=config),
            patch("bot.telegram_bot.TelegramConfig.from_env", return_value=telegram),
            patch("bot.telegram_bot.configure_logging"),
            patch("bot.telegram_bot.build_product_verifier_service", return_value=object()),
            patch("bot.telegram_bot.build_job_manager", return_value=object()),
            patch("bot.telegram_bot.validate_runtime_wiring"),
            patch("bot.telegram_bot.build_application", return_value=Application()),
            patch("bot.telegram_bot.log_event") as event,
        ):
            bot_main()
        names = [call.args[2] for call in event.call_args_list]
        self.assertIn("bot_starting", names)
        self.assertNotIn(telegram.bot_token, repr(event.call_args_list))


if __name__ == "__main__":
    unittest.main()
