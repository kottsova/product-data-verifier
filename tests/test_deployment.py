"""Static and runtime-safe Stage 16 deployment tests (no live network)."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
import logging
import os
from pathlib import Path
import re
import signal
import tempfile
import unittest
from unittest.mock import patch

from bot.telegram_bot import STOP_SIGNALS, main as bot_main, validate_runtime_wiring
from config import AppConfig, ConfigurationError, TelegramConfig
from healthcheck import HEALTHY, UNHEALTHY, build_health_snapshot, main as health_main
from services.product_verifier import ProductVerifierService


ROOT = Path(__file__).resolve().parent.parent


class HealthcheckTests(unittest.TestCase):
    def test_health_succeeds_with_nested_writable_sqlite_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "nested" / "health.sqlite3"
            snapshot = build_health_snapshot({"PRODUCT_VERIFIER_DB_PATH": str(db_path)})
            self.assertEqual(snapshot["status"], HEALTHY)
            self.assertEqual(snapshot["checks"], {
                "config": True, "repository": True, "diagnostics": True,
            })
            self.assertTrue(db_path.is_file())
            diagnostics = snapshot["diagnostics"]
            self.assertTrue(diagnostics["repository_available"])
            self.assertEqual(diagnostics["versions"]["cache_schema"], 1)

    def test_health_fails_cleanly_when_db_path_is_a_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = StringIO()
            with redirect_stdout(output):
                status = health_main(env={"PRODUCT_VERIFIER_DB_PATH": tmp})
            payload = json.loads(output.getvalue())
            self.assertEqual(status, 1)
            self.assertEqual(payload["status"], UNHEALTHY)
            self.assertEqual(payload["error"], "repository_unavailable")
            self.assertNotIn(tmp, output.getvalue())
            self.assertNotIn("Traceback", output.getvalue())

    def test_health_output_is_machine_readable_and_never_contains_token(self):
        token = "123456:health-secret"
        with tempfile.TemporaryDirectory() as tmp:
            output = StringIO()
            with redirect_stdout(output):
                status = health_main(env={
                    "PRODUCT_VERIFIER_DB_PATH": str(Path(tmp) / "health.sqlite3"),
                    "TELEGRAM_BOT_TOKEN": token,
                })
            payload = json.loads(output.getvalue())
            self.assertEqual(status, 0)
            self.assertEqual(payload["status"], HEALTHY)
            self.assertNotIn(token, output.getvalue())

    def test_health_does_not_run_product_verification(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            ProductVerifierService, "verify", side_effect=AssertionError("must not run"),
        ) as verify:
            snapshot = build_health_snapshot({
                "PRODUCT_VERIFIER_DB_PATH": str(Path(tmp) / "health.sqlite3"),
            })
        self.assertEqual(snapshot["status"], HEALTHY)
        verify.assert_not_called()

    def test_invalid_central_app_config_is_reported_as_unhealthy(self):
        snapshot = build_health_snapshot({"PRODUCT_VERIFIER_LOG_LEVEL": "LOUD"})
        self.assertEqual(snapshot["status"], UNHEALTHY)
        self.assertEqual(snapshot["error"], "invalid_configuration")
        self.assertFalse(snapshot["checks"]["config"])


class ProductionStartupTests(unittest.TestCase):
    def test_missing_telegram_token_fails_before_runtime_wiring(self):
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("bot.telegram_bot.configure_logging"),
            patch("bot.telegram_bot.log_event") as event,
            patch("bot.telegram_bot.build_product_verifier_service") as build_service,
            self.assertRaises(SystemExit) as raised,
        ):
            bot_main()
        self.assertEqual(raised.exception.code, 1)
        build_service.assert_not_called()
        failure = [call for call in event.call_args_list if call.args[2] == "bot_configuration_failure"]
        self.assertEqual(failure[0].kwargs["reason"], "missing_or_invalid_telegram_token")

    def test_unusable_database_fails_before_polling_without_token_leak(self):
        token = "123456:startup-secret"
        old_handlers = list(logging.getLogger().handlers)
        old_level = logging.getLogger().level
        try:
            with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {
                "TELEGRAM_BOT_TOKEN": token,
                "PRODUCT_VERIFIER_DB_PATH": tmp,
            }, clear=True), patch("bot.telegram_bot.build_application") as build_application:
                stderr = StringIO()
                with redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
                    bot_main()
                self.assertEqual(raised.exception.code, 1)
                build_application.assert_not_called()
                self.assertIn("bot_startup_failure", stderr.getvalue())
                self.assertNotIn(token, stderr.getvalue())
        finally:
            root = logging.getLogger()
            for handler in list(root.handlers):
                root.removeHandler(handler)
            for handler in old_handlers:
                root.addHandler(handler)
            root.setLevel(old_level)

    def test_polling_receives_sigint_and_sigterm(self):
        class Application:
            kwargs = None

            def run_polling(self, **kwargs):
                self.kwargs = kwargs

        application = Application()
        config = AppConfig(db_path="unused.sqlite3")
        telegram = TelegramConfig(bot_token="123:signal-secret")
        with (
            patch("bot.telegram_bot.AppConfig.from_env", return_value=config),
            patch("bot.telegram_bot.TelegramConfig.from_env", return_value=telegram),
            patch("bot.telegram_bot.configure_logging"),
            patch("bot.telegram_bot.build_product_verifier_service", return_value=object()),
            patch("bot.telegram_bot.build_job_manager", return_value=object()),
            patch("bot.telegram_bot.validate_runtime_wiring"),
            patch("bot.telegram_bot.build_application", return_value=application),
            patch("bot.telegram_bot.log_event"),
        ):
            bot_main()
        self.assertEqual(application.kwargs["stop_signals"], (signal.SIGINT, signal.SIGTERM))
        self.assertEqual(STOP_SIGNALS, (signal.SIGINT, signal.SIGTERM))

    def test_runtime_wiring_rejects_unavailable_configured_repository(self):
        class Diagnostics:
            def to_dict(self):
                return {"repository": {"configured": True, "available": False}}

        class Manager:
            def diagnostics(self):
                return Diagnostics()

        with self.assertRaises(RuntimeError):
            validate_runtime_wiring(Manager())


class DeploymentFileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        cls.dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")
        cls.compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")

    def test_dockerfile_uses_exact_python_version_and_runtime_lock(self):
        match = re.search(r"(?mi)^FROM\s+python:(\d+\.\d+\.\d+)-slim-", self.dockerfile)
        self.assertIsNotNone(match)
        self.assertNotIn(":latest", self.dockerfile.casefold())
        self.assertIn("requirements.lock", self.dockerfile)

    def test_dockerfile_installs_browser_and_runs_as_non_root(self):
        self.assertRegex(self.dockerfile, r"playwright\s+install[^\n]*chromium")
        users = re.findall(r"(?mi)^USER\s+([^\s]+)", self.dockerfile)
        self.assertTrue(users)
        self.assertNotIn(users[-1].casefold(), {"root", "0", "0:0"})
        self.assertIn("chown app:app /data", self.dockerfile)

    def test_dockerfile_has_healthcheck_and_single_production_command(self):
        self.assertIn('CMD ["python", "-m", "healthcheck"]', self.dockerfile)
        commands = re.findall(r"(?mi)^CMD\s+(.+)$", self.dockerfile)
        self.assertEqual(commands[-1], '["python", "-m", "bot.telegram_bot"]')
        self.assertIn("STOPSIGNAL SIGTERM", self.dockerfile)

    def test_dockerignore_excludes_secrets_runtime_state_and_local_artifacts(self):
        for required in (
            ".git", ".env", ".venv", ".vscode", ".cache", "__pycache__",
            "*.sqlite3", "*.db", "*.log", "credentials.json", "tests",
        ):
            self.assertIn(required, self.dockerignore)

    def test_compose_uses_runtime_env_and_persistent_data_volume(self):
        for variable in (
            "TELEGRAM_BOT_TOKEN",
            "PRODUCT_VERIFIER_DB_PATH",
            "PRODUCT_VERIFIER_MAX_CONCURRENT_JOBS",
            "PRODUCT_VERIFIER_JOB_HISTORY_LIMIT",
            "PRODUCT_VERIFIER_CACHE_TTL_SECONDS",
            "PRODUCT_VERIFIER_LOG_LEVEL",
            "PRODUCT_VERIFIER_LOG_FORMAT",
        ):
            self.assertIn(variable, self.compose)
        self.assertIn("/data/product-verifier.sqlite3", self.compose)
        self.assertRegex(self.compose, r"product-verifier-data:/data")
        self.assertIn("restart: unless-stopped", self.compose)
        self.assertIn("stop_grace_period: 45s", self.compose)

    def test_compose_contains_only_environment_reference_not_a_token(self):
        self.assertIn("${TELEGRAM_BOT_TOKEN:?", self.compose)
        self.assertIsNone(re.search(r"(?m)^\s*TELEGRAM_BOT_TOKEN:\s*\d+:[A-Za-z0-9_-]+", self.compose))

    def test_healthcheck_uses_central_config_and_no_direct_environment_access(self):
        source = (ROOT / "healthcheck.py").read_text(encoding="utf-8")
        self.assertIn("AppConfig.from_env", source)
        self.assertNotIn("os.environ", source)
        self.assertNotIn("os.getenv", source)
        self.assertNotIn("from config import TelegramConfig", source)

    def test_runtime_lock_contains_only_exact_pins(self):
        lines = [
            line.strip() for line in (ROOT / "requirements.lock").read_text().splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        self.assertTrue(lines)
        self.assertTrue(all(re.fullmatch(r"[A-Za-z0-9_.-]+==[^=<>~!]+", line) for line in lines))
        for runtime_dependency in (
            "playwright", "pypdf", "beautifulsoup4", "requests", "python-telegram-bot",
        ):
            self.assertTrue(any(line.casefold().startswith(runtime_dependency + "==") for line in lines))


if __name__ == "__main__":
    unittest.main()
