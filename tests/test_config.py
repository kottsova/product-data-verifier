"""Stage 14 configuration/secrets tests.

Covers the central config.py boundary: env parsing, validation, the
CLI-vs-Telegram config split, and secret hygiene (the bot token must never
appear in a repr, an error message, or a diagnostic dict). Also enforces --
via a lightweight AST scan -- that production modules no longer read
os.environ/os.getenv directly now that config.py is the single boundary.
"""

from __future__ import annotations

import ast
from pathlib import Path
import unittest

from config import (
    AppConfig,
    ConfigurationError,
    TelegramConfig,
)


# ---------------------------------------------------------------------------
# AppConfig: general settings, never requires a Telegram secret
# ---------------------------------------------------------------------------

class AppConfigDefaultsTests(unittest.TestCase):
    def test_defaults_when_environment_is_empty(self):
        config = AppConfig.from_env(env={})
        self.assertTrue(config.db_path.endswith("product_verifier.sqlite3"))
        self.assertEqual(config.max_concurrent_jobs, 2)
        self.assertEqual(config.job_history_limit, 20)
        self.assertEqual(config.cache_ttl_seconds, 3600.0)

    def test_loading_general_config_never_requires_a_telegram_token(self):
        # This is the boundary that keeps `python app.py` working with no
        # TELEGRAM_BOT_TOKEN set at all -- AppConfig.from_env must not raise.
        config = AppConfig.from_env(env={})
        self.assertIsInstance(config, AppConfig)


class AppConfigDbPathTests(unittest.TestCase):
    def test_honors_the_environment_variable(self):
        self.assertEqual(
            AppConfig.from_env(env={"PRODUCT_VERIFIER_DB_PATH": "/tmp/custom.sqlite3"}).db_path,
            "/tmp/custom.sqlite3",
        )

    def test_relative_path_is_preserved_as_given(self):
        self.assertEqual(
            AppConfig.from_env(env={"PRODUCT_VERIFIER_DB_PATH": "./data/verifier.sqlite3"}).db_path,
            "./data/verifier.sqlite3",
        )

    def test_blank_value_falls_back_to_the_default(self):
        config = AppConfig.from_env(env={"PRODUCT_VERIFIER_DB_PATH": "   "})
        self.assertTrue(config.db_path.endswith("product_verifier.sqlite3"))


class AppConfigConcurrencyTests(unittest.TestCase):
    def test_honors_the_environment_variable(self):
        config = AppConfig.from_env(env={"PRODUCT_VERIFIER_MAX_CONCURRENT_JOBS": "5"})
        self.assertEqual(config.max_concurrent_jobs, 5)

    def test_rejects_non_integer_value(self):
        with self.assertRaises(ConfigurationError):
            AppConfig.from_env(env={"PRODUCT_VERIFIER_MAX_CONCURRENT_JOBS": "abc"})

    def test_rejects_zero(self):
        with self.assertRaises(ConfigurationError):
            AppConfig.from_env(env={"PRODUCT_VERIFIER_MAX_CONCURRENT_JOBS": "0"})

    def test_rejects_negative(self):
        with self.assertRaises(ConfigurationError):
            AppConfig.from_env(env={"PRODUCT_VERIFIER_MAX_CONCURRENT_JOBS": "-3"})

    def test_direct_construction_also_validates(self):
        with self.assertRaises(ConfigurationError):
            AppConfig(max_concurrent_jobs=0)


class AppConfigJobHistoryTests(unittest.TestCase):
    def test_honors_the_environment_variable(self):
        config = AppConfig.from_env(env={"PRODUCT_VERIFIER_JOB_HISTORY_LIMIT": "50"})
        self.assertEqual(config.job_history_limit, 50)

    def test_accepts_zero(self):
        config = AppConfig.from_env(env={"PRODUCT_VERIFIER_JOB_HISTORY_LIMIT": "0"})
        self.assertEqual(config.job_history_limit, 0)

    def test_rejects_negative(self):
        with self.assertRaises(ConfigurationError):
            AppConfig.from_env(env={"PRODUCT_VERIFIER_JOB_HISTORY_LIMIT": "-1"})

    def test_rejects_non_integer(self):
        with self.assertRaises(ConfigurationError):
            AppConfig.from_env(env={"PRODUCT_VERIFIER_JOB_HISTORY_LIMIT": "soon"})


class AppConfigCacheTtlTests(unittest.TestCase):
    def test_honors_the_environment_variable(self):
        config = AppConfig.from_env(env={"PRODUCT_VERIFIER_CACHE_TTL_SECONDS": "120"})
        self.assertEqual(config.cache_ttl_seconds, 120.0)

    def test_accepts_a_fractional_value(self):
        config = AppConfig.from_env(env={"PRODUCT_VERIFIER_CACHE_TTL_SECONDS": "0.5"})
        self.assertEqual(config.cache_ttl_seconds, 0.5)

    def test_accepts_zero(self):
        config = AppConfig.from_env(env={"PRODUCT_VERIFIER_CACHE_TTL_SECONDS": "0"})
        self.assertEqual(config.cache_ttl_seconds, 0.0)

    def test_rejects_negative(self):
        with self.assertRaises(ConfigurationError):
            AppConfig.from_env(env={"PRODUCT_VERIFIER_CACHE_TTL_SECONDS": "-1"})

    def test_rejects_non_numeric(self):
        with self.assertRaises(ConfigurationError):
            AppConfig.from_env(env={"PRODUCT_VERIFIER_CACHE_TTL_SECONDS": "soon"})


# ---------------------------------------------------------------------------
# TelegramConfig: the one runtime secret, loaded and validated separately
# ---------------------------------------------------------------------------

class TelegramConfigTests(unittest.TestCase):
    def test_missing_token_fails_fast_with_a_clear_message(self):
        with self.assertRaises(ConfigurationError) as context:
            TelegramConfig.from_env(env={})
        self.assertIn("TELEGRAM_BOT_TOKEN", str(context.exception))

    def test_blank_token_also_fails_fast(self):
        with self.assertRaises(ConfigurationError):
            TelegramConfig.from_env(env={"TELEGRAM_BOT_TOKEN": "   "})

    def test_present_token_is_loaded(self):
        config = TelegramConfig.from_env(env={"TELEGRAM_BOT_TOKEN": "123:abc-secret"})
        self.assertEqual(config.bot_token, "123:abc-secret")


# ---------------------------------------------------------------------------
# Secret hygiene: the token must never leak through repr/to_dict/errors
# ---------------------------------------------------------------------------

class SecretHygieneTests(unittest.TestCase):
    SECRET = "123456:super-secret-token-value"

    def test_token_never_appears_in_repr(self):
        config = TelegramConfig(bot_token=self.SECRET)
        self.assertNotIn(self.SECRET, repr(config))

    def test_token_never_appears_in_str(self):
        config = TelegramConfig(bot_token=self.SECRET)
        self.assertNotIn(self.SECRET, str(config))

    def test_token_never_appears_in_to_dict(self):
        config = TelegramConfig(bot_token=self.SECRET)
        self.assertNotIn(self.SECRET, repr(config.to_dict()))
        self.assertNotIn(self.SECRET, str(config.to_dict()))

    def test_missing_token_error_message_cannot_contain_a_secret(self):
        # There is no secret to leak when the token is *missing*, but the
        # error message must still never echo back a caller-supplied blank
        # value verbatim in a way that could later carry one.
        with self.assertRaises(ConfigurationError) as context:
            TelegramConfig.from_env(env={"TELEGRAM_BOT_TOKEN": "   "})
        self.assertNotIn(self.SECRET, str(context.exception))

    def test_app_config_never_holds_or_leaks_a_token(self):
        # AppConfig has no token field at all -- it cannot leak what it
        # doesn't hold, which is the point of splitting it from TelegramConfig.
        config = AppConfig.from_env(env={"TELEGRAM_BOT_TOKEN": self.SECRET})
        self.assertNotIn(self.SECRET, repr(config))
        self.assertNotIn(self.SECRET, repr(config.to_dict()))
        self.assertNotIn("bot_token", config.to_dict())


# ---------------------------------------------------------------------------
# Source boundary: no scattered os.environ/os.getenv outside config.py
# ---------------------------------------------------------------------------

class EnvironmentReadingBoundaryTests(unittest.TestCase):
    """Stage 14: application settings are read in exactly one place.

    core/discovery.py and core/fetch.py are a deliberate, pre-existing
    exception: PDV_BROWSER_HEADLESS is a Stage 1-9 browser-automation detail,
    not one of the application settings this stage centralizes, and touching
    it is out of this stage's scope. reference/ holds standalone legacy
    scraper scripts that are not part of the running application.
    """

    PRODUCTION_FILES = (
        "app.py",
        "bot/handlers.py",
        "bot/formatters.py",
        "bot/parser.py",
        "bot/telegram_bot.py",
        "bot/service_factory.py",
        "bot/jobs.py",
        "services/cache.py",
        "services/product_verifier.py",
    )

    def _reads_os_environment(self, source: str) -> bool:
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in ("environ", "getenv"):
                target = node.value
                if isinstance(target, ast.Name) and target.id == "os":
                    return True
            if isinstance(node, ast.ImportFrom) and node.module == "os":
                if any(alias.name in ("environ", "getenv") for alias in node.names):
                    return True
        return False

    def test_no_production_module_reads_the_environment_directly(self):
        root = Path(__file__).resolve().parent.parent
        offenders = []
        for relative_path in self.PRODUCTION_FILES:
            source = (root / relative_path).read_text(encoding="utf-8")
            if self._reads_os_environment(source):
                offenders.append(relative_path)
        self.assertEqual(
            offenders, [],
            f"these production modules read os.environ/os.getenv directly, "
            f"bypassing config.py: {offenders}",
        )

    def test_config_module_itself_is_the_only_env_reading_boundary(self):
        root = Path(__file__).resolve().parent.parent
        source = (root / "config.py").read_text(encoding="utf-8")
        self.assertTrue(self._reads_os_environment(source))


if __name__ == "__main__":
    unittest.main()
