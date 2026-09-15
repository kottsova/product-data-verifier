"""Stage 14 central configuration and secrets boundary.

This is the only module in the codebase allowed to read process environment
variables for application settings. Everything downstream -- production
wiring in ``bot/service_factory.py``, ``bot/telegram_bot.py``, and any future
consumer -- receives a typed, already-validated config object instead of
reading ``os.environ``/``os.getenv`` itself.

Two separate types reflect two separate lifetimes:

- ``AppConfig`` holds settings that make sense with no Telegram context at
  all (DB path, job concurrency/retention, cache TTL). ``AppConfig.from_env``
  never requires a Telegram secret, so a CLI-only run (``python app.py``)
  is never blocked by a missing bot token.
- ``TelegramConfig`` holds the one Telegram-specific runtime secret. It is
  loaded separately, only by the Telegram entry point, and is never included
  in a repr, a to_dict(), a log line, or an error message.

Both raise ``ConfigurationError`` -- never a generic ``ValueError`` -- so a
caller can fail fast, before any polling or persistence I/O starts, on any
missing or malformed setting.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
from typing import Mapping


class ConfigurationError(Exception):
    """Raised for any missing or invalid application configuration/secret."""


TELEGRAM_BOT_TOKEN_ENV_VAR = "TELEGRAM_BOT_TOKEN"
DB_PATH_ENV_VAR = "PRODUCT_VERIFIER_DB_PATH"
MAX_CONCURRENT_JOBS_ENV_VAR = "PRODUCT_VERIFIER_MAX_CONCURRENT_JOBS"
JOB_HISTORY_LIMIT_ENV_VAR = "PRODUCT_VERIFIER_JOB_HISTORY_LIMIT"
CACHE_TTL_SECONDS_ENV_VAR = "PRODUCT_VERIFIER_CACHE_TTL_SECONDS"

DEFAULT_DB_PATH = str(Path(__file__).resolve().parent / ".cache" / "product_verifier.sqlite3")
DEFAULT_MAX_CONCURRENT_JOBS = 2
DEFAULT_JOB_HISTORY_LIMIT = 20
DEFAULT_CACHE_TTL_SECONDS = 3600.0


def _source(env: Mapping[str, str] | None) -> Mapping[str, str]:
    return env if env is not None else os.environ


def _resolve_int(
    source: Mapping[str, str], var_name: str, default: int, *, minimum: int,
) -> int:
    raw = source.get(var_name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip())
    except ValueError as error:
        raise ConfigurationError(f"{var_name} must be an integer, got {raw!r}") from error
    if value < minimum:
        raise ConfigurationError(f"{var_name} must be >= {minimum}, got {value}")
    return value


def _resolve_float(
    source: Mapping[str, str], var_name: str, default: float, *, minimum: float,
) -> float:
    raw = source.get(var_name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw.strip())
    except ValueError as error:
        raise ConfigurationError(f"{var_name} must be a number, got {raw!r}") from error
    if value < minimum:
        raise ConfigurationError(f"{var_name} must be >= {minimum}, got {value}")
    return value


def _resolve_db_path(source: Mapping[str, str]) -> str:
    raw = (source.get(DB_PATH_ENV_VAR) or "").strip()
    return raw or DEFAULT_DB_PATH


@dataclass(frozen=True, slots=True)
class AppConfig:
    """Typed, validated application settings. Never holds a secret.

    Safe to load with no Telegram context (e.g. the CLI): ``from_env`` never
    reads or requires ``TELEGRAM_BOT_TOKEN``.
    """

    db_path: str = DEFAULT_DB_PATH
    max_concurrent_jobs: int = DEFAULT_MAX_CONCURRENT_JOBS
    job_history_limit: int = DEFAULT_JOB_HISTORY_LIMIT
    cache_ttl_seconds: float = DEFAULT_CACHE_TTL_SECONDS

    def __post_init__(self) -> None:
        if not self.db_path.strip():
            raise ConfigurationError(f"{DB_PATH_ENV_VAR} must not be blank")
        if self.max_concurrent_jobs < 1:
            raise ConfigurationError(
                f"{MAX_CONCURRENT_JOBS_ENV_VAR} must be >= 1, got {self.max_concurrent_jobs}"
            )
        if self.job_history_limit < 0:
            raise ConfigurationError(
                f"{JOB_HISTORY_LIMIT_ENV_VAR} must be >= 0, got {self.job_history_limit}"
            )
        if self.cache_ttl_seconds < 0:
            raise ConfigurationError(
                f"{CACHE_TTL_SECONDS_ENV_VAR} must be >= 0, got {self.cache_ttl_seconds}"
            )

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "AppConfig":
        source = _source(env)
        return cls(
            db_path=_resolve_db_path(source),
            max_concurrent_jobs=_resolve_int(
                source, MAX_CONCURRENT_JOBS_ENV_VAR, DEFAULT_MAX_CONCURRENT_JOBS, minimum=1,
            ),
            job_history_limit=_resolve_int(
                source, JOB_HISTORY_LIMIT_ENV_VAR, DEFAULT_JOB_HISTORY_LIMIT, minimum=0,
            ),
            cache_ttl_seconds=_resolve_float(
                source, CACHE_TTL_SECONDS_ENV_VAR, DEFAULT_CACHE_TTL_SECONDS, minimum=0.0,
            ),
        )

    def to_dict(self) -> dict[str, object]:
        """JSON-safe, loggable view. Contains no secret -- AppConfig never holds one."""
        return {
            "db_path": self.db_path,
            "max_concurrent_jobs": self.max_concurrent_jobs,
            "job_history_limit": self.job_history_limit,
            "cache_ttl_seconds": self.cache_ttl_seconds,
        }


@dataclass(frozen=True, slots=True)
class TelegramConfig:
    """The one Telegram runtime secret. Deliberately its own type.

    ``bot_token`` is excluded from the dataclass-generated ``repr`` (so
    ``repr(config)``/``print(config)`` never leaks it), and ``to_dict`` never
    returns the raw value either -- only whether one is set.
    """

    bot_token: str = field(repr=False)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "TelegramConfig":
        source = _source(env)
        token = (source.get(TELEGRAM_BOT_TOKEN_ENV_VAR) or "").strip()
        if not token:
            raise ConfigurationError(
                f"{TELEGRAM_BOT_TOKEN_ENV_VAR} is not set. Set it before starting the bot, e.g.:\n"
                f'  {TELEGRAM_BOT_TOKEN_ENV_VAR}="123456:ABC..." python -m bot.telegram_bot'
            )
        return cls(bot_token=token)

    def to_dict(self) -> dict[str, object]:
        """Diagnostic-safe view: never the token itself, only whether one is set."""
        return {"bot_token_set": bool(self.bot_token)}
