"""Production wiring: Telegram bot -> ProductVerifierService -> SQLite.

No hidden global singleton: ``build_product_verifier_service`` returns a
fresh, fully-configured instance each call. The caller (``telegram_bot.main``
or a test) owns its lifetime and can pass a fake/in-memory setup instead.
"""

from __future__ import annotations

from pathlib import Path
import os
from typing import Mapping

from services.cache import SqliteProductVerificationRepository
from services.product_verifier import ProductVerifierService


DB_PATH_ENV_VAR = "PRODUCT_VERIFIER_DB_PATH"
DEFAULT_DB_PATH = str(
    Path(__file__).resolve().parent.parent / ".cache" / "product_verifier.sqlite3"
)


def resolve_db_path(env: Mapping[str, str] | None = None) -> str:
    """Configurable SQLite path: PRODUCT_VERIFIER_DB_PATH, else a repo-local default."""
    source = env if env is not None else os.environ
    return source.get(DB_PATH_ENV_VAR) or DEFAULT_DB_PATH


def build_product_verifier_service(
    *,
    db_path: str | None = None,
    env: Mapping[str, str] | None = None,
) -> ProductVerifierService:
    """Build the real, SQLite-cached service used by the running bot."""
    path = db_path or resolve_db_path(env)
    repository = SqliteProductVerificationRepository(path)
    return ProductVerifierService(repository=repository)
