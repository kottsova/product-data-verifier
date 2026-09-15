"""Deterministic Stage 12 Telegram bot tests. No live network/Telegram API."""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from bot.formatters import (
    ERROR_MESSAGES,
    QUALITY_LABELS,
    chunk_lines,
    format_error,
    format_result,
)
from bot.handlers import (
    HELP_MESSAGE,
    PARSE_ERROR_MESSAGE,
    START_MESSAGE,
    build_verify_command,
    handle_product_query,
    help_command,
    start_command,
)
from bot.parser import ParsedProductQuery, parse_product_query
from bot.service_factory import (
    DB_PATH_ENV_VAR,
    build_product_verifier_service,
    resolve_db_path,
)
from bot.telegram_bot import MissingBotTokenError, resolve_bot_token
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


class HandleProductQueryTests(unittest.TestCase):
    def _run(self, coro):
        import asyncio
        return asyncio.run(coro)

    def test_invalid_input_never_calls_the_service(self):
        service = TrackingFakeService(make_result())
        reply = RecordingReply()
        self._run(handle_product_query("just-one-token", service, reply=reply))
        self.assertEqual(service.calls, [])
        self.assertEqual(reply.messages, [PARSE_ERROR_MESSAGE])

    def test_valid_input_builds_a_verify_product_request_and_formats_the_reply(self):
        service = TrackingFakeService(make_result(status="verified"))
        reply = RecordingReply()
        self._run(handle_product_query("Bosch PUE611BB5E", service, reply=reply))
        self.assertEqual(len(service.calls), 1)
        self.assertEqual(service.calls[0].brand, "Bosch")
        self.assertEqual(service.calls[0].model, "PUE611BB5E")
        self.assertTrue(reply.messages)
        self.assertIn("verified", reply.messages[0])

    def test_a_raising_fake_service_produces_a_safe_internal_error_reply(self):
        class BrokenService:
            def verify(self, request):
                raise KeyError("boom")

        reply = RecordingReply()
        self._run(handle_product_query("Bosch PUE611BB5E", BrokenService(), reply=reply))
        self.assertEqual(len(reply.messages), 1)
        self.assertNotIn("boom", reply.messages[0])
        self.assertNotIn("KeyError", reply.messages[0])

    def test_real_product_verifier_service_wiring_with_fake_workflow(self):
        """Confirms handle_product_query works against the real service type,
        not just a duck-typed fake -- with a fake workflow runner so no live
        network is touched."""
        profile = final_profile([definition("power")], [candidate("power", "1000", unit="W")])
        service = ProductVerifierService(run_workflow=fake_runner(profile))
        reply = RecordingReply()
        self._run(handle_product_query("Acme X100", service, reply=reply))
        self.assertTrue(reply.messages)
        self.assertIn("power", "\n".join(reply.messages).casefold())

    def test_workflow_failure_from_the_real_service_is_formatted_safely(self):
        service = ProductVerifierService(run_workflow=raising_runner(RuntimeError("net down")))
        reply = RecordingReply()
        self._run(handle_product_query("Acme X100", service, reply=reply))
        self.assertEqual(len(reply.messages), 1)
        self.assertIn("net down", reply.messages[0])
        self.assertNotIn("workflow_failure", reply.messages[0])


# ---------------------------------------------------------------------------
# Telegram-facing commands (fake Update/Message, no PTB network)
# ---------------------------------------------------------------------------

class FakeMessage:
    def __init__(self, text: str | None = None, *, fail_send: bool = False):
        self.text = text
        self.sent: list[str] = []
        self._fail_send = fail_send

    async def reply_text(self, text: str) -> None:
        if self._fail_send:
            raise ConnectionError("Telegram API unreachable")
        self.sent.append(text)


class FakeUpdate:
    def __init__(self, message: FakeMessage):
        self.message = message


class TelegramCommandTests(unittest.TestCase):
    def _run(self, coro):
        import asyncio
        return asyncio.run(coro)

    def test_start_command_sends_the_start_message(self):
        message = FakeMessage()
        self._run(start_command(FakeUpdate(message), None))
        self.assertEqual(message.sent, [START_MESSAGE])

    def test_help_command_sends_the_help_message(self):
        message = FakeMessage()
        self._run(help_command(FakeUpdate(message), None))
        self.assertEqual(message.sent, [HELP_MESSAGE])

    def test_verify_command_uses_the_injected_service_and_replies(self):
        service = TrackingFakeService(make_result(status="partial"))
        verify_command = build_verify_command(service)
        message = FakeMessage(text="Bosch PUE611BB5E")
        self._run(verify_command(FakeUpdate(message), None))
        self.assertEqual(len(service.calls), 1)
        self.assertTrue(message.sent)
        self.assertIn("partial", message.sent[0])

    def test_verify_command_on_parse_failure_never_calls_the_service(self):
        service = TrackingFakeService(make_result())
        verify_command = build_verify_command(service)
        message = FakeMessage(text="onlyonetoken")
        self._run(verify_command(FakeUpdate(message), None))
        self.assertEqual(service.calls, [])
        self.assertEqual(message.sent, [PARSE_ERROR_MESSAGE])

    def test_send_failure_is_caught_at_the_adapter_and_does_not_raise(self):
        message = FakeMessage(fail_send=True)
        try:
            self._run(start_command(FakeUpdate(message), None))
        except Exception as error:  # pragma: no cover - failure path under test
            self.fail(f"start_command must not propagate a send failure, got {error!r}")
        self.assertEqual(message.sent, [])  # the send failed, but silently


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
        "bot/telegram_bot.py", "bot/service_factory.py",
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
