"""Stage 17 final deterministic Telegram-to-service wiring regression.

No Telegram API or live web is used.  These tests deliberately exercise the
real handler, JobManager, ProductVerifierService cache boundary, and formatter
together; detailed component edge cases remain in test_bot*.py.
"""

from __future__ import annotations

import asyncio
import unittest

from bot.formatters import format_accepted, format_duplicate, format_started
from bot.handlers import _start_verification, handle_product_query
from bot.jobs import JobManager
from services.cache import InMemoryProductVerificationRepository
from services.product_verifier import ProductVerifierService, VerifyProductRequest
from tests.test_bot_jobs import GatedService
from tests.test_product_verifier import raising_runner
from tests.test_profile_export import candidate, definition, final_profile


class ReplyRecorder:
    def __init__(self):
        self.messages: list[str] = []

    async def __call__(self, message: str) -> None:
        self.messages.append(message)


class IgnoredPromptLanguage:
    """Stage 31.1: the language prompt itself is out of scope here -- see
    test_bot.py's HandleProductQueryTests for that flow. This suite always
    picks the default (RU) via ``ask_and_start`` to keep its existing
    Russian-text assertions."""

    async def __call__(self, text: str, buttons: list[tuple[str, str]]) -> None:
        return None


async def ask_and_start(
    text: str, chat_id: int, manager: JobManager, *, reply, language: str = "ru",
) -> None:
    """Drive both Stage 31.1 steps at once: parse+prompt, then pick a language."""
    await handle_product_query(
        text, chat_id, manager, reply=reply, prompt_language=IgnoredPromptLanguage(),
    )
    request = manager.pop_pending_query(chat_id)
    if request is None:
        return  # invalid input never reached the prompt -- nothing to start
    await _start_verification(request, chat_id, manager, language=language, reply=reply)


async def wait_until_idle(manager: JobManager, chat_id: int, timeout: float = 2.0) -> None:
    async def poll() -> None:
        while manager.active_jobs_for_chat(chat_id):
            await asyncio.sleep(0.01)

    await asyncio.wait_for(poll(), timeout=timeout)


class FinalMvpWiringTests(unittest.IsolatedAsyncioTestCase):
    async def _run_profile(self, profile, *, chat_id: int) -> str:
        service = ProductVerifierService(
            run_workflow=lambda _request: type(
                "WorkflowResult", (), {"final_profile": profile},
            )(),
        )
        manager = JobManager(service, max_concurrent_jobs=1)
        reply = ReplyRecorder()
        await ask_and_start("Acme X100", chat_id, manager, reply=reply)
        await wait_until_idle(manager, chat_id)
        return "\n".join(reply.messages)

    async def test_accepted_running_final_and_cache_hit_cross_the_complete_path(self):
        profile = final_profile(
            [definition("power")], [candidate("power", "1000", unit="W")],
        )
        workflow_calls = []

        def fake_workflow(request):
            workflow_calls.append(request)
            return type("WorkflowResult", (), {"final_profile": profile})()

        service = ProductVerifierService(
            run_workflow=fake_workflow,
            repository=InMemoryProductVerificationRepository(),
        )
        manager = JobManager(service, max_concurrent_jobs=1)
        request = VerifyProductRequest(brand="Acme", model="X100")

        first = ReplyRecorder()
        await ask_and_start("Acme X100", 17, manager, reply=first)
        await wait_until_idle(manager, 17)
        self.assertIn(format_accepted(request), first.messages)
        self.assertIn(format_started(request), first.messages)
        self.assertTrue(any("power" in message.casefold() for message in first.messages))

        second = ReplyRecorder()
        await ask_and_start("Acme X100", 17, manager, reply=second)
        await wait_until_idle(manager, 17)
        self.assertEqual(len(workflow_calls), 1)
        self.assertTrue(any("кэш" in message.casefold() for message in second.messages))

    async def test_duplicate_and_cancellation_keep_one_active_computation(self):
        service = GatedService()
        manager = JobManager(service, max_concurrent_jobs=1)
        first, duplicate = ReplyRecorder(), ReplyRecorder()
        await ask_and_start("Bosch PUE611BB5E", 18, manager, reply=first)
        self.assertTrue(await service.wait_started("Bosch", "PUE611BB5E"))
        active_job = manager.active_jobs_for_chat(18)[0]

        await ask_and_start("Bosch PUE611BB5E", 18, manager, reply=duplicate)
        self.assertEqual(duplicate.messages, [format_duplicate(active_job)])
        self.assertEqual(len(service.calls), 1)

        self.assertEqual(await manager.cancel_chat_jobs(18), 1)
        service.release_all()
        await wait_until_idle(manager, 18)
        self.assertEqual(active_job.state, "cancelled")

    async def test_workflow_failure_is_formatted_without_leaking_error_kind(self):
        service = ProductVerifierService(
            run_workflow=raising_runner(RuntimeError("stage17 network down")),
        )
        manager = JobManager(service, max_concurrent_jobs=1)
        reply = ReplyRecorder()
        await ask_and_start("Acme X100", 19, manager, reply=reply)
        await wait_until_idle(manager, 19)
        joined = "\n".join(reply.messages)
        self.assertIn("stage17 network down", joined)
        self.assertNotIn("workflow_failure", joined)

    async def test_insufficient_result_names_unresolved_fields_and_explains_itself(self):
        profile = final_profile([
            definition("power"),
            definition("timer"),
        ])
        joined = await self._run_profile(profile, chat_id=20)
        self.assertNotIn("insufficient", joined)
        self.assertIn("Недостаточно данных", joined)
        self.assertIn("Не определено (2)", joined)
        self.assertIn("power", joined.casefold())
        self.assertIn("timer", joined.casefold())
        self.assertIn("Почему данных недостаточно", joined)

    async def test_conflicted_result_keeps_confirmed_conflict_and_unresolved_distinct(self):
        profile = final_profile(
            [
                definition("brand", priority="critical"),
                definition("model", priority="critical"),
                definition("power"),
                definition("voltage", priority="critical"),
                definition("timer"),
            ],
            [
                candidate("brand", "Acme"),
                candidate("model", "X100"),
                candidate("power", "1000", unit="W"),
                candidate("voltage", "220", unit="V", source="https://one.example/X100"),
                candidate("voltage", "110", unit="V", source="https://two.example/X100"),
            ],
        )
        joined = await self._run_profile(profile, chat_id=21)
        self.assertNotIn("conflicted", joined)
        self.assertIn("расходятся", joined)
        self.assertIn("Подтверждено (3)", joined)
        self.assertIn("Конфликты (1)", joined)
        self.assertIn("Не определено (1)", joined)
        self.assertIn("power: 1000 w", joined.casefold())
        self.assertIn("voltage: данные расходятся", joined.casefold())
        self.assertIn("timer", joined.casefold())


if __name__ == "__main__":
    unittest.main()
