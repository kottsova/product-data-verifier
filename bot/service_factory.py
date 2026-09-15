"""Production wiring: Telegram bot -> ProductVerifierService -> SQLite.

No hidden global singleton: ``build_product_verifier_service`` returns a
fresh, fully-configured instance each call. The caller (``telegram_bot.main``
or a test) owns its lifetime and passes in the already-loaded/validated
``AppConfig`` -- this module never reads the environment itself (Stage 14).
"""

from __future__ import annotations

from config import AppConfig
from services.cache import CachePolicy, SqliteProductVerificationRepository
from services.product_verifier import ProductVerifierService


def build_product_verifier_service(config: AppConfig) -> ProductVerifierService:
    """Build the real, SQLite-cached service used by the running bot."""
    repository = SqliteProductVerificationRepository(config.db_path)
    cache_policy = CachePolicy(ttl_seconds=config.cache_ttl_seconds)
    return ProductVerifierService(repository=repository, cache_policy=cache_policy)
