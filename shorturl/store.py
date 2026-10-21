"""SQLite persistence for short links.

One connection is shared by all server threads and guarded by a lock. That is
simpler than a connection per request and is the only option for an in-memory
database, which exists once per connection.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Callable, Iterator, Optional

from .codec import code_for_row_id

# AUTOINCREMENT matters: it guarantees a deleted row's id is never handed out
# again, which ``_insert_generated`` relies on to skip ids safely.
_SCHEMA = """
CREATE TABLE IF NOT EXISTS links (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    code       TEXT UNIQUE,
    url        TEXT NOT NULL,
    clicks     INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
)
"""


class CodeTakenError(Exception):
    """The requested custom code already belongs to another link."""


@dataclass(frozen=True)
class Link:
    """One stored short link; ``created_at`` is seconds since the Unix epoch."""

    code: str
    url: str
    clicks: int
    created_at: float


class LinkStore:
    """Stores links and hands out unique codes. Safe to call from many threads."""

    def __init__(
        self, path: str = ":memory:", clock: Callable[[], float] = time.time
    ) -> None:
        """Open (or create) the database at ``path``; ``clock`` stamps new links."""
        self._clock = clock
        self._lock = threading.Lock()
        # isolation_level=None turns off sqlite3's implicit transactions so the
        # explicit BEGIN/COMMIT below are the only ones.
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.execute(_SCHEMA)

    def add(self, url: str, custom_code: Optional[str] = None) -> Link:
        """Store ``url`` under ``custom_code`` or a generated code and return it.

        Raises ``CodeTakenError`` if ``custom_code`` is already in use.
        """
        created_at = self._clock()
        with self._write_transaction() as db:
            if custom_code is None:
                code = self._insert_generated(db, url, created_at)
            else:
                code = custom_code
                try:
                    db.execute(
                        "INSERT INTO links (code, url, created_at) VALUES (?, ?, ?)",
                        (code, url, created_at),
                    )
                except sqlite3.IntegrityError as error:
                    raise CodeTakenError(code) from error
        return Link(code, url, 0, created_at)

    def resolve(self, code: str) -> Optional[str]:
        """Return the target URL for ``code`` and count the click, or ``None``.

        The increment and the lookup are one statement, so concurrent clicks
        cannot lose updates and a click is never counted for a missing code.
        """
        with self._lock:
            rows = self._db.execute(
                "UPDATE links SET clicks = clicks + 1 WHERE code = ? RETURNING url",
                (code,),
            ).fetchall()
        return rows[0][0] if rows else None

    def stats(self, code: str) -> Optional[Link]:
        """Return the link for ``code`` without counting a click, or ``None``."""
        with self._lock:
            row = self._db.execute(
                "SELECT code, url, clicks, created_at FROM links WHERE code = ?",
                (code,),
            ).fetchone()
        return Link(*row) if row else None

    def close(self) -> None:
        """Close the database connection."""
        with self._lock:
            self._db.close()

    @contextmanager
    def _write_transaction(self) -> Iterator[sqlite3.Connection]:
        """Run the body atomically; BEGIN IMMEDIATE takes the write lock up front."""
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield self._db
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            self._db.execute("COMMIT")

    @staticmethod
    def _insert_generated(db: sqlite3.Connection, url: str, created_at: float) -> str:
        """Insert ``url`` under the code derived from its own row id.

        A custom code may equal a code the generator has not issued yet. When
        that happens this row id is burned (deleted, never reused) and the next
        id is tried, so each generated code still comes from exactly one row id.
        """
        while True:
            row_id = db.execute(
                "INSERT INTO links (url, created_at) VALUES (?, ?)", (url, created_at)
            ).lastrowid
            code = code_for_row_id(row_id)
            if db.execute("SELECT 1 FROM links WHERE code = ?", (code,)).fetchone() is None:
                db.execute("UPDATE links SET code = ? WHERE id = ?", (code, row_id))
                return code
            db.execute("DELETE FROM links WHERE id = ?", (row_id,))
