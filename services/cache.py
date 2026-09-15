"""Stage 11 storage abstraction for cached VerifyProductResult payloads.

This module knows nothing about the pipeline, the schema, validation, or
quality policy -- it only stores and retrieves JSON-safe payloads keyed by an
opaque string, plus enough metadata (schema_version, stored_at, success) for
the service layer to decide freshness and compatibility. Persistence is
intentionally kept out of core/ (Stage 1-9) entirely.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
import json
from pathlib import Path
import sqlite3
from typing import Mapping, Protocol


CACHE_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class CacheEntry:
    """One stored record: an opaque key, a JSON-safe payload, and metadata."""

    key: str
    schema_version: int
    stored_at: float
    success: bool
    payload: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class CachePolicy:
    """Centralized, testable freshness/caching policy. Not category-specific."""

    ttl_seconds: float = 3600.0
    cache_failures: bool = False

    def __post_init__(self) -> None:
        if self.ttl_seconds < 0:
            raise ValueError("ttl_seconds must be non-negative")


DEFAULT_CACHE_POLICY = CachePolicy()


class ProductVerificationRepository(Protocol):
    """Storage contract the service depends on -- never a concrete database."""

    def get(self, key: str) -> CacheEntry | None: ...

    def save(self, entry: CacheEntry) -> None: ...

    def delete(self, key: str) -> None: ...

    def clear(self) -> None: ...


class InMemoryProductVerificationRepository:
    """A process-local fake for tests. Never used for real persistence."""

    def __init__(self) -> None:
        self._entries: dict[str, CacheEntry] = {}

    def get(self, key: str) -> CacheEntry | None:
        return self._entries.get(key)

    def save(self, entry: CacheEntry) -> None:
        self._entries[entry.key] = entry

    def delete(self, key: str) -> None:
        self._entries.pop(key, None)

    def clear(self) -> None:
        self._entries.clear()


_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS verification_cache (
    cache_key TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL,
    stored_at REAL NOT NULL,
    success INTEGER NOT NULL,
    payload TEXT NOT NULL
)
"""


class SqliteProductVerificationRepository:
    """MVP persistent backend: stdlib sqlite3, no external dependency.

    Stores only the JSON-safe VerifyProductResult payload -- never pickle,
    never an internal pipeline object. A fresh connection is opened per call
    (this is a low-throughput MVP store, not a connection-pooled service),
    which also keeps it safe to use from multiple short-lived processes.
    """

    def __init__(self, path: str | Path) -> None:
        self._path = str(path)
        parent = Path(self._path).parent
        if str(parent) not in ("", "."):
            parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.execute(_TABLE_SQL)

    @contextlib.contextmanager
    def _connection(self):
        connection = sqlite3.connect(self._path)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def get(self, key: str) -> CacheEntry | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT schema_version, stored_at, success, payload "
                "FROM verification_cache WHERE cache_key = ?",
                (key,),
            ).fetchone()
        if row is None:
            return None
        schema_version, stored_at, success, payload_text = row
        try:
            payload = json.loads(payload_text)
        except json.JSONDecodeError:
            return None
        if not isinstance(payload, dict):
            return None
        return CacheEntry(
            key=key,
            schema_version=int(schema_version),
            stored_at=float(stored_at),
            success=bool(success),
            payload=payload,
        )

    def save(self, entry: CacheEntry) -> None:
        payload_text = json.dumps(dict(entry.payload), ensure_ascii=False)
        with self._connection() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO verification_cache "
                "(cache_key, schema_version, stored_at, success, payload) "
                "VALUES (?, ?, ?, ?, ?)",
                (entry.key, entry.schema_version, entry.stored_at, int(entry.success), payload_text),
            )

    def delete(self, key: str) -> None:
        with self._connection() as connection:
            connection.execute("DELETE FROM verification_cache WHERE cache_key = ?", (key,))

    def clear(self) -> None:
        with self._connection() as connection:
            connection.execute("DELETE FROM verification_cache")
