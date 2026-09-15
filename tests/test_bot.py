"""Deterministic Stage 12/13 Telegram bot tests. No live network/Telegram API.

JobManager's own lifecycle/concurrency/cancellation/shutdown behavior has a
dedicated, more thorough suite in tests/test_bot_jobs.py. This file covers
the parser, formatters (including the Stage 13 job-lifecycle helpers), the
Telegram-facing handlers wired to a JobManager, the architecture boundary,
and configuration/wiring.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
import tempfile
import unittest

from bot.formatters import (
    ERROR_MESSAGES,
    QUALITY_LABELS,
    chunk_lines,
    format_accepted,
    format_cancelled,
    format_duplicate,
    format_error,
    format_job_outcome,
    format_result,
    format_started,
    format_status,
)
from bot.handlers import (
    HELP_MESSAGE,
    PARSE_ERROR_MESSAGE,
    START_MESSAGE,
    build_cancel_command,
    build_status_command,
    build_verify_command,
    handle_product_query,
    help_command,
    start_command,
)
from bot.jobs import Job, JobManager
from bot.parser import ParsedProductQuery, parse_product_query
from bot.service_factory import (
    DB_PATH_ENV_VAR,
    build_product_verifier_service,
    resolve_db_path,
)
from bot.telegram_bot import (
    InvalidJobConfigurationError,
    MissingBotTokenError,
    build_job_manager,
    resolve_bot_token,
    resolve_job_history_limit,
    resolve_max_concurrent_jobs,
)
from services.cache import SqliteProductVerificationRepository
from services.product_verifier import (
    ProductVerifierService,
    ServiceAttribute,
    ServiceCategory,
    ServiceIdentity,
    ServiceQuality,
    VerifyProductError,
    VerifyProductRequest,
    VerifyProductResult,
)
from tests.test_product_verifier import fake_runner, raising_runner
from tests.test_profile_export import candidate, definition, final_profile


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

class ParserTests(unittest.TestCase):
    def test_plain_two_tokens(self):
        self.assertEqual(
            parse_product_query("Bosch PUE611BB5E"),
            ParsedProductQuery(brand="Bosch", model="PUE611BB5E"),
        )

    def test_plain_multi_word_model_is_joined(self):
        self.assertEqual(
            parse_product_query("Dreame G12 Pro HHR32A"),
            ParsedProductQuery(brand="Dreame", model="G12 Pro HHR32A"),
        )

    def test_pipe_two_parts(self):
        self.assertEqual(
            parse_product_query("Bosch | PUE611BB5E"),
            ParsedProductQuery(brand="Bosch", model="PUE611BB5E"),
        )

    def test_pipe_three_parts_includes_article(self):
        self.assertEqual(
            parse_product_query("Dreame | G12 Pro | HHR32A"),
            ParsedProductQuery(brand="Dreame", model="G12 Pro", article="HHR32A"),
        )

    def test_extra_whitespace_is_collapsed(self):
        self.assertEqual(
            parse_product_query("  Bosch    PUE611BB5E  "),
            ParsedProductQuery(brand="Bosch", model="PUE611BB5E"),
        )

    def test_single_token_is_invalid(self):
        self.assertIsNone(parse_product_query("Bosch"))

    def test_empty_text_is_invalid(self):
        self.assertIsNone(parse_product_query(""))
        self.assertIsNone(parse_product_query("   "))

    def test_pipe_with_too_many_parts_is_invalid(self):
        self.assertIsNone(parse_product_query("Bosch | X | Y | Z"))

    def test_pipe_with_empty_part_is_invalid(self):
        self.assertIsNone(parse_product_query("Bosch |  | Y"))
        self.assertIsNone(parse_product_query("Bosch |"))


# ---------------------------------------------------------------------------
# Formatters
# ---------------------------------------------------------------------------

def make_result(
    *,
    status="verified",
    served_from_cache=False,
    cache_age_seconds=None,
    attribute_count=3,
    success=True,
    error=None,
):
    if not success:
        return VerifyProductResult(
            success=False,
            request=VerifyProductRequest(brand="Acme", model="X100"),
            error=error,
        )
    identity = ServiceIdentity(
        brand="Acme", base_model="X100", commercial_model="X100",
        manufacturer_article=None, product_code=None, sku=None, gtin=None,
        color=None, configuration={}, confidence="high",
    )
    category = ServiceCategory(
        category_id="cooktop", category_name="Cooktop", parent_category="major_appliance",
        confidence="high",
    )
    quality = ServiceQuality(
        status=status, coverage_percent=75.0, schema_total=10, schema_found=8,
        confirmed_count=6, unresolved_count=2, conflict_count=1, critical_total=3,
        critical_found=3, critical_confirmed=2, critical_conflict=0,
        critical_high_authority_confirmed=2, category_confidence="high",
        identity_confidence="high", reasons=("some reason",), warnings=(),
    )
    attributes = tuple(
        ServiceAttribute(
            canonical_name=f"attr_{index}", display_name=f"Attr {index}",
            value=f"value-{index}", unit=None, status="Confirmed", confidence="high",
            source="https://example.test", evidence="evidence text",
            priority="high", expected=True, discovered=False,
        )
        for index in range(attribute_count)
    )
    return VerifyProductResult(
        success=True,
        request=VerifyProductRequest(brand="Acme", model="X100"),
        identity=identity, category=category, attributes=attributes,
        quality=quality, metadata={},
        served_from_cache=served_from_cache, cache_stored_at=1000.0 if served_from_cache else None,
        cache_age_seconds=cache_age_seconds,
    )


class FormatterQualityStatusTests(unittest.TestCase):
    def test_all_four_statuses_have_distinct_human_labels(self):
        labels = {status: QUALITY_LABELS[status] for status in
                   ("verified", "partial", "insufficient", "conflicted")}
        self.assertEqual(len(set(labels.values())), 4)

    def test_conflicted_status_is_shown_not_hidden(self):
        result = make_result(status="conflicted")
        text = "\n".join(format_result(result))
        self.assertIn("conflicted", text)
        self.assertIn("противоречия", text)

    def test_insufficient_status_is_shown_not_hidden(self):
        result = make_result(status="insufficient")
        text = "\n".join(format_result(result))
        self.assertIn("insufficient", text)
        self.assertIn("Недостаточно", text)

    def test_verified_and_partial_are_distinguishable(self):
        verified_text = "\n".join(format_result(make_result(status="verified")))
        partial_text = "\n".join(format_result(make_result(status="partial")))
        self.assertNotEqual(verified_text.split("\n")[2], partial_text.split("\n")[2])


class FormatterCacheIndicatorTests(unittest.TestCase):
    def test_cache_hit_is_indicated(self):
        result = make_result(served_from_cache=True, cache_age_seconds=125)
        text = "\n".join(format_result(result))
        self.assertIn("кэша", text)
        self.assertIn("2 мин", text)

    def test_live_result_has_no_cache_indicator(self):
        result = make_result(served_from_cache=False)
        text = "\n".join(format_result(result))
        self.assertNotIn("кэша", text)


class FormatterErrorTests(unittest.TestCase):
    def test_invalid_request_error_is_user_friendly(self):
        error = VerifyProductError(kind="invalid_request", message="brand and model are required")
        result = make_result(success=False, error=error)
        messages = format_result(result)
        self.assertEqual(len(messages), 1)
        self.assertIn("brand and model are required", messages[0])
        self.assertNotIn("invalid_request", messages[0])

    def test_workflow_failure_error_is_user_friendly(self):
        error = VerifyProductError(kind="workflow_failure", message="network down", detail="RuntimeError")
        result = make_result(success=False, error=error)
        messages = format_result(result)
        self.assertIn("network down", messages[0])
        self.assertNotIn("workflow_failure", messages[0])
        self.assertNotIn("RuntimeError", messages[0])

    def test_internal_error_never_leaks_exception_detail(self):
        error = VerifyProductError(
            kind="internal_error", message="Unexpected failure: boom", detail="KeyError",
        )
        result = make_result(success=False, error=error)
        messages = format_result(result)
        self.assertNotIn("internal_error", messages[0])
        self.assertNotIn("KeyError", messages[0])
        self.assertNotIn("boom", messages[0])
        self.assertNotIn("Traceback", messages[0])

    def test_error_message_dict_covers_every_error_kind(self):
        self.assertEqual(
            set(ERROR_MESSAGES), {"invalid_request", "workflow_failure", "internal_error"},
        )


class FormatterLengthLimitTests(unittest.TestCase):
    def test_many_attributes_are_capped_with_a_remainder_note(self):
        result = make_result(attribute_count=40)
        messages = format_result(result, max_attributes=12)
        joined = "\n".join(messages)
        self.assertIn("ещё", joined)
        self.assertLessEqual(joined.count("Attr "), 12)

    def test_no_message_chunk_exceeds_the_configured_limit(self):
        result = make_result(attribute_count=200)
        messages = format_result(result, max_attributes=200, max_message_length=500)
        self.assertGreater(len(messages), 1)
        for message in messages:
            self.assertLessEqual(len(message), 500)

    def test_no_message_ever_exceeds_the_telegram_hard_limit(self):
        result = make_result(attribute_count=500)
        messages = format_result(result, max_attributes=500)
        for message in messages:
            self.assertLess(len(message), 4096)

    def test_chunk_lines_never_splits_a_single_short_line(self):
        lines = ["short one", "short two", "short three"]
        chunks = chunk_lines(lines, max_length=15)
        for chunk in chunks:
            for line in lines:
                self.assertNotIn(line[:5] + "\n" + line[5:], chunk)

    def test_chunk_lines_truncates_a_single_oversized_line(self):
        chunks = chunk_lines(["x" * 100], max_length=20)
        self.assertEqual(len(chunks), 1)
        self.assertLessEqual(len(chunks[0]), 20)


def make_job(*, state="queued", request=None, result=None, error=None) -> Job:
    return Job(
        id="job-1", chat_id=1, request=request or VerifyProductRequest(brand="Bosch", model="PUE611BB5E"),
        state=state, result=result, error=error,
    )


class Stage13JobFormatterTests(unittest.TestCase):
    def test_format_accepted_names_the_product(self):
        text = format_accepted(VerifyProductRequest(brand="Bosch", model="PUE611BB5E"))
        self.assertIn("Bosch", text)
        self.assertIn("PUE611BB5E", text)

    def test_format_started_names_the_product(self):
        text = format_started(VerifyProductRequest(brand="Bosch", model="PUE611BB5E"))
        self.assertIn("Bosch", text)
        self.assertIn("PUE611BB5E", text)

    def test_format_duplicate_mentions_the_product_and_its_state(self):
        job = make_job(state="running")
        text = format_duplicate(job)
        self.assertIn("Bosch", text)
        self.assertIn("выполняется", text)

    def test_format_status_lists_every_active_job(self):
        jobs = [
            make_job(request=VerifyProductRequest(brand="Bosch", model="A"), state="queued"),
            make_job(request=VerifyProductRequest(brand="HONOR", model="B"), state="running"),
        ]
        text = format_status(jobs)
        self.assertIn("Bosch", text)
        self.assertIn("HONOR", text)
        self.assertIn("очеред", text)
        self.assertIn("выполня", text)

    def test_format_status_handles_no_active_jobs(self):
        text = format_status([])
        self.assertTrue(text)
        self.assertNotIn("None", text)

    def test_format_cancelled_zero_vs_nonzero(self):
        self.assertNotEqual(format_cancelled(0), format_cancelled(1))
        self.assertIn("1", format_cancelled(1))
        self.assertIn("3", format_cancelled(3))

    def test_format_job_outcome_for_completed_job_reuses_format_result(self):
        result = make_result(status="verified")
        job = make_job(state="completed", result=result)
        self.assertEqual(format_job_outcome(job), format_result(result))

    def test_format_job_outcome_for_failed_job_with_result_reuses_format_result(self):
        error = VerifyProductError(kind="workflow_failure", message="net down")
        result = make_result(success=False, error=error)
        job = make_job(state="failed", result=result, error="net down")
        self.assertEqual(format_job_outcome(job), format_result(result))

    def test_format_job_outcome_for_failed_job_without_result_is_generic_and_safe(self):
        job = make_job(state="failed", result=None, error="KeyError: boom")
        messages = format_job_outcome(job)
        self.assertEqual(len(messages), 1)
        self.assertNotIn("KeyError", messages[0])
        self.assertNotIn("boom", messages[0])

    def test_format_job_outcome_for_cancelled_job_is_empty(self):
        # JobManager._finalize() never attaches a result for a "cancelled" job
        # (the underlying computation's result, if any, is discarded before
        # this point) -- format_job_outcome must still produce no message
        # even if a caller somehow constructs a cancelled Job with a result.
        job = make_job(state="cancelled", result=None)
        self.assertEqual(format_job_outcome(job), [])
        job_with_stray_result = make_job(state="cancelled", result=make_result(status="verified"))
        self.assertEqual(format_job_outcome(job_with_stray_result), [])


# ---------------------------------------------------------------------------
# Handlers (framework-free core: handle_product_query)
# ---------------------------------------------------------------------------

class RecordingReply:
    def __init__(self):
        self.messages: list[str] = []

    async def __call__(self, text: str) -> None:
        self.messages.append(text)


class TrackingFakeService:
    """A minimal fake matching ProductVerifierService's public shape."""

    def __init__(self, result: VerifyProductResult):
        self._result = result
        self.calls: list[VerifyProductRequest] = []

    def verify(self, request: VerifyProductRequest) -> VerifyProductResult:
        self.calls.append(request)
        return self._result


class HandleProductQueryTests(unittest.IsolatedAsyncioTestCase):
    """Stage 13: handle_product_query submits a background job and replies
    immediately; the final result arrives asynchronously through the same
    ``reply`` via the job's on_update callback."""

    async def test_invalid_input_never_creates_a_job(self):
        service = TrackingFakeService(make_result())
        manager = JobManager(service, max_concurrent_jobs=1)
        reply = RecordingReply()
        await handle_product_query("just-one-token", 1, manager, reply=reply)
        self.assertEqual(service.calls, [])
        self.assertEqual(reply.messages, [PARSE_ERROR_MESSAGE])
        self.assertEqual(manager.active_jobs_for_chat(1), [])

    async def test_valid_input_replies_immediately_with_an_accepted_message(self):
        service = TrackingFakeService(make_result(status="verified"))
        manager = JobManager(service, max_concurrent_jobs=1)
        reply = RecordingReply()
        await handle_product_query("Bosch PUE611BB5E", 1, manager, reply=reply)
        self.assertTrue(reply.messages)
        self.assertIn("Bosch", reply.messages[0])
        self.assertIn("PUE611BB5E", reply.messages[0])

    async def test_final_result_arrives_via_the_same_reply_once_the_job_completes(self):
        service = TrackingFakeService(make_result(status="verified"))
        manager = JobManager(service, max_concurrent_jobs=1)
        reply = RecordingReply()
        await handle_product_query("Bosch PUE611BB5E", 1, manager, reply=reply)
        # Give the scheduled background task a chance to run to completion.
        for _ in range(50):
            if len(reply.messages) >= 2:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(len(service.calls), 1)
        self.assertTrue(any("verified" in message for message in reply.messages))

    async def test_a_raising_fake_service_produces_a_safe_reply_not_a_crash(self):
        class BrokenService:
            def verify(self, request):
                raise KeyError("boom")

        manager = JobManager(BrokenService(), max_concurrent_jobs=1)
        reply = RecordingReply()
        await handle_product_query("Bosch PUE611BB5E", 1, manager, reply=reply)
        for _ in range(50):
            if len(reply.messages) >= 2:
                break
            await asyncio.sleep(0.01)
        joined = "\n".join(reply.messages)
        self.assertNotIn("boom", joined)
        self.assertNotIn("KeyError", joined)

    async def test_real_product_verifier_service_wiring_with_fake_workflow(self):
        """Confirms handle_product_query works against the real service type,
        not just a duck-typed fake -- with a fake workflow runner so no live
        network is touched."""
        profile = final_profile([definition("power")], [candidate("power", "1000", unit="W")])
        service = ProductVerifierService(run_workflow=fake_runner(profile))
        manager = JobManager(service, max_concurrent_jobs=1)
        reply = RecordingReply()
        await handle_product_query("Acme X100", 1, manager, reply=reply)
        for _ in range(50):
            if len(reply.messages) >= 2:
                break
            await asyncio.sleep(0.01)
        self.assertIn("power", "\n".join(reply.messages).casefold())

    async def test_workflow_failure_from_the_real_service_is_formatted_safely(self):
        service = ProductVerifierService(run_workflow=raising_runner(RuntimeError("net down")))
        manager = JobManager(service, max_concurrent_jobs=1)
        reply = RecordingReply()
        await handle_product_query("Acme X100", 1, manager, reply=reply)
        for _ in range(50):
            if len(reply.messages) >= 2:
                break
            await asyncio.sleep(0.01)
        joined = "\n".join(reply.messages)
        self.assertIn("net down", joined)
        self.assertNotIn("workflow_failure", joined)

    async def test_duplicate_active_request_replies_without_a_second_job(self):
        service = TrackingFakeService(make_result(status="verified"))
        manager = JobManager(service, max_concurrent_jobs=1)
        reply1, reply2 = RecordingReply(), RecordingReply()
        await handle_product_query("Bosch PUE611BB5E", 1, manager, reply=reply1)
        await handle_product_query("Bosch PUE611BB5E", 1, manager, reply=reply2)
        self.assertTrue(any("уже выполняется" in message for message in reply2.messages))


# ---------------------------------------------------------------------------
# Telegram-facing commands (fake Update/Message, no PTB network)
# ---------------------------------------------------------------------------

class FakeMessage:
    def __init__(self, text: str | None = None, *, chat_id: int = 42, fail_send: bool = False):
        self.text = text
        self.chat_id = chat_id
        self.sent: list[str] = []
        self._fail_send = fail_send

    async def reply_text(self, text: str) -> None:
        if self._fail_send:
            raise ConnectionError("Telegram API unreachable")
        self.sent.append(text)


class FakeUpdate:
    def __init__(self, message: FakeMessage):
        self.message = message


class TelegramCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_command_sends_the_start_message(self):
        message = FakeMessage()
        await start_command(FakeUpdate(message), None)
        self.assertEqual(message.sent, [START_MESSAGE])

    async def test_help_command_sends_the_help_message(self):
        message = FakeMessage()
        await help_command(FakeUpdate(message), None)
        self.assertEqual(message.sent, [HELP_MESSAGE])
        self.assertIn("/status", HELP_MESSAGE)
        self.assertIn("/cancel", HELP_MESSAGE)

    async def test_verify_command_submits_a_job_and_replies(self):
        service = TrackingFakeService(make_result(status="partial"))
        manager = JobManager(service, max_concurrent_jobs=1)
        verify_command = build_verify_command(manager)
        message = FakeMessage(text="Bosch PUE611BB5E")
        await verify_command(FakeUpdate(message), None)
        self.assertTrue(message.sent)
        self.assertIn("Bosch", message.sent[0])

    async def test_verify_command_on_parse_failure_never_creates_a_job(self):
        service = TrackingFakeService(make_result())
        manager = JobManager(service, max_concurrent_jobs=1)
        verify_command = build_verify_command(manager)
        message = FakeMessage(text="onlyonetoken")
        await verify_command(FakeUpdate(message), None)
        self.assertEqual(service.calls, [])
        self.assertEqual(message.sent, [PARSE_ERROR_MESSAGE])

    async def test_send_failure_is_caught_at_the_adapter_and_does_not_raise(self):
        message = FakeMessage(fail_send=True)
        try:
            await start_command(FakeUpdate(message), None)
        except Exception as error:  # pragma: no cover - failure path under test
            self.fail(f"start_command must not propagate a send failure, got {error!r}")
        self.assertEqual(message.sent, [])  # the send failed, but silently

    async def test_status_command_reports_no_active_jobs(self):
        manager = JobManager(TrackingFakeService(make_result()), max_concurrent_jobs=1)
        status_command = build_status_command(manager)
        message = FakeMessage(chat_id=7)
        await status_command(FakeUpdate(message), None)
        self.assertEqual(len(message.sent), 1)
        self.assertNotIn("Bosch", message.sent[0])

    async def test_status_command_lists_an_active_job_for_the_same_chat(self):
        from tests.test_bot_jobs import GatedService

        service = GatedService()
        manager = JobManager(service, max_concurrent_jobs=1)
        verify_command = build_verify_command(manager)
        status_command = build_status_command(manager)
        message = FakeMessage(text="Bosch PUE611BB5E", chat_id=7)
        await verify_command(FakeUpdate(message), None)
        await service.wait_started("Bosch", "PUE611BB5E")

        status_message = FakeMessage(chat_id=7)
        await status_command(FakeUpdate(status_message), None)
        self.assertIn("Bosch", status_message.sent[0])

        service.release_all()

    async def test_cancel_command_reports_zero_with_nothing_active(self):
        manager = JobManager(TrackingFakeService(make_result()), max_concurrent_jobs=1)
        cancel_command = build_cancel_command(manager)
        message = FakeMessage(chat_id=9)
        await cancel_command(FakeUpdate(message), None)
        self.assertIn("Нет", message.sent[0])

    async def test_cancel_command_cancels_an_active_job_for_the_same_chat(self):
        from tests.test_bot_jobs import GatedService

        service = GatedService()
        manager = JobManager(service, max_concurrent_jobs=1)
        verify_command = build_verify_command(manager)
        cancel_command = build_cancel_command(manager)
        message = FakeMessage(text="Bosch PUE611BB5E", chat_id=9)
        await verify_command(FakeUpdate(message), None)
        await service.wait_started("Bosch", "PUE611BB5E")

        cancel_message = FakeMessage(chat_id=9)
        await cancel_command(FakeUpdate(cancel_message), None)
        self.assertIn("Отменен", cancel_message.sent[0])
        # The job is flagged, but a running job's blocking call cannot be
        # force-killed -- it only leaves the active list once it finalizes.
        active_before_release = manager.active_jobs_for_chat(9)
        self.assertEqual(len(active_before_release), 1)
        self.assertTrue(active_before_release[0].cancel_requested)

        service.release_all()
        for _ in range(50):
            if not manager.active_jobs_for_chat(9):
                break
            await asyncio.sleep(0.01)
        self.assertEqual(manager.active_jobs_for_chat(9), [])


# ---------------------------------------------------------------------------
# Architecture: the bot layer must depend only on the stable service boundary
# ---------------------------------------------------------------------------

class ArchitectureBoundaryTests(unittest.TestCase):
    """The bot layer's *code* (imports and identifiers) must never reach into
    core.* pipeline internals -- only prose (docstrings/comments) may mention
    them for explanation, so this walks the AST rather than grep'ing text."""

    FORBIDDEN_MODULE_PREFIXES = ("core.workflow", "core.profile", "core.quality", "core.validation")
    FORBIDDEN_NAMES = {
        "ProductWorkflowResult", "FinalProductProfile", "QualityAssessment",
        "run_product_workflow", "ValidatedFact", "ValidatedProductProfile",
    }
    BOT_MODULES = (
        "bot/handlers.py", "bot/formatters.py", "bot/parser.py",
        "bot/telegram_bot.py", "bot/service_factory.py", "bot/jobs.py",
    )

    def _code_identifiers_and_imports(self, source: str) -> tuple[set[str], set[str]]:
        import ast

        tree = ast.parse(source)
        imported_modules: set[str] = set()
        names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_modules.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_modules.add(node.module)
                names.update(alias.name for alias in node.names)
            elif isinstance(node, ast.Name):
                names.add(node.id)
            elif isinstance(node, ast.Attribute):
                names.add(node.attr)
        return imported_modules, names

    def test_handler_and_formatter_layers_never_reference_core_pipeline_types(self):
        root = Path(__file__).resolve().parent.parent
        for relative_path in self.BOT_MODULES:
            source = (root / relative_path).read_text(encoding="utf-8")
            modules, names = self._code_identifiers_and_imports(source)
            for module in modules:
                for forbidden in self.FORBIDDEN_MODULE_PREFIXES:
                    self.assertFalse(
                        module == forbidden or module.startswith(forbidden + "."),
                        f"{relative_path} imports from {module!r}, which touches "
                        f"a forbidden internal module ({forbidden!r})",
                    )
            offending_names = names & self.FORBIDDEN_NAMES
            self.assertEqual(
                offending_names, set(),
                f"{relative_path} references forbidden internal identifiers: {offending_names}",
            )


# ---------------------------------------------------------------------------
# Configuration and production wiring (no real Telegram/network)
# ---------------------------------------------------------------------------

class ConfigurationTests(unittest.TestCase):
    def test_missing_token_fails_fast_with_a_clear_message(self):
        with self.assertRaises(MissingBotTokenError) as context:
            resolve_bot_token(env={})
        self.assertIn("TELEGRAM_BOT_TOKEN", str(context.exception))

    def test_blank_token_also_fails_fast(self):
        with self.assertRaises(MissingBotTokenError):
            resolve_bot_token(env={"TELEGRAM_BOT_TOKEN": "   "})

    def test_present_token_is_returned(self):
        self.assertEqual(
            resolve_bot_token(env={"TELEGRAM_BOT_TOKEN": "123:abc"}), "123:abc",
        )

    def test_db_path_defaults_when_env_var_absent(self):
        path = resolve_db_path(env={})
        self.assertTrue(path.endswith("product_verifier.sqlite3"))

    def test_db_path_honors_the_environment_variable(self):
        custom = str(Path(tempfile.gettempdir()) / "custom_verifier.sqlite3")
        self.assertEqual(resolve_db_path(env={DB_PATH_ENV_VAR: custom}), custom)

    def test_max_concurrent_jobs_defaults_when_absent(self):
        self.assertEqual(resolve_max_concurrent_jobs(env={}), 2)

    def test_max_concurrent_jobs_honors_the_environment_variable(self):
        self.assertEqual(
            resolve_max_concurrent_jobs(env={"PRODUCT_VERIFIER_MAX_CONCURRENT_JOBS": "5"}), 5,
        )

    def test_max_concurrent_jobs_rejects_non_integer(self):
        with self.assertRaises(InvalidJobConfigurationError):
            resolve_max_concurrent_jobs(env={"PRODUCT_VERIFIER_MAX_CONCURRENT_JOBS": "abc"})

    def test_max_concurrent_jobs_rejects_zero_or_negative(self):
        with self.assertRaises(InvalidJobConfigurationError):
            resolve_max_concurrent_jobs(env={"PRODUCT_VERIFIER_MAX_CONCURRENT_JOBS": "0"})

    def test_job_history_limit_defaults_when_absent(self):
        self.assertEqual(resolve_job_history_limit(env={}), 20)

    def test_job_history_limit_honors_the_environment_variable(self):
        self.assertEqual(
            resolve_job_history_limit(env={"PRODUCT_VERIFIER_JOB_HISTORY_LIMIT": "50"}), 50,
        )

    def test_job_history_limit_accepts_zero(self):
        self.assertEqual(resolve_job_history_limit(env={"PRODUCT_VERIFIER_JOB_HISTORY_LIMIT": "0"}), 0)

    def test_job_history_limit_rejects_negative(self):
        with self.assertRaises(InvalidJobConfigurationError):
            resolve_job_history_limit(env={"PRODUCT_VERIFIER_JOB_HISTORY_LIMIT": "-1"})

    def test_job_history_limit_rejects_non_integer(self):
        with self.assertRaises(InvalidJobConfigurationError):
            resolve_job_history_limit(env={"PRODUCT_VERIFIER_JOB_HISTORY_LIMIT": "soon"})

    def test_build_job_manager_wires_configured_limits(self):
        service = TrackingFakeService(make_result())
        manager = build_job_manager(
            service, env={"PRODUCT_VERIFIER_MAX_CONCURRENT_JOBS": "3", "PRODUCT_VERIFIER_JOB_HISTORY_LIMIT": "5"},
        )
        self.assertIsInstance(manager, JobManager)
        self.assertEqual(manager._history_limit, 5)
        self.assertEqual(manager._semaphore._value, 3)


class ServiceFactoryWiringTests(unittest.TestCase):
    def test_factory_wires_a_real_sqlite_repository_without_any_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "wiring.sqlite3")
            service = build_product_verifier_service(db_path=db_path)
            self.assertIsInstance(service._repository, SqliteProductVerificationRepository)
            self.assertTrue(Path(db_path).exists())

    def test_each_factory_call_builds_an_independent_service_instance(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "wiring.sqlite3")
            first = build_product_verifier_service(db_path=db_path)
            second = build_product_verifier_service(db_path=db_path)
            self.assertIsNot(first, second)
            self.assertIsNot(first._repository, second._repository)


if __name__ == "__main__":
    unittest.main()
