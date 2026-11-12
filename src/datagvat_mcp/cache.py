"""Answer cache: SQLite on disk (survives restarts) or in memory.

Entries belong to a category (search, dataset, catalogue, vocabulary, preview).
Each category has its own lifetime and its own size limit; when a category is
full, its least recently used entries go first. An entry past its lifetime is
kept for a while longer as a "stale" copy, to be served only when the origin
cannot be reached.
"""

import contextvars
import json
import logging
import os
import sqlite3
import time
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

CATEGORIES = ("search", "dataset", "catalogue", "vocabulary", "preview")
SCHEMA_VERSION = 1
MEMORY_LIMIT = 16 * 1024 * 1024  # per category when there is no cache directory
DB_FILE = "cache.sqlite3"


@dataclass
class Hit:
    value: Any
    stored_at: float  # wall-clock time (time.time) the answer was fetched
    fresh: bool  # False: past its lifetime, only good as a fallback


class DiskCache:
    def __init__(
        self,
        directory: str | None,
        limits: dict[str, int],
        max_stale: float = 0.0,
        clock: Callable[[], float] = time.time,
    ):
        self._clock = clock
        self.max_stale = max_stale
        self.persistent = False
        self._db = self._open(directory)
        cap = None if self.persistent else MEMORY_LIMIT
        self.limits = {c: max(0, limits.get(c, 0) if cap is None else min(limits.get(c, 0), cap)) for c in CATEGORIES}
        self.purge()

    # ---------------------------------------------------------------- setup

    def _open(self, directory: str | None) -> sqlite3.Connection:
        if directory:
            path = os.path.join(directory, DB_FILE)
            for attempt in (1, 2):
                try:
                    os.makedirs(directory, exist_ok=True)
                    db = self._connect(path)
                    self.persistent = True
                    return db
                except (OSError, sqlite3.Error) as e:
                    if attempt == 1 and isinstance(e, sqlite3.DatabaseError) and os.path.exists(path):
                        logger.warning("Cache file %s is unusable (%s); starting a new one", path, e)
                        try:
                            os.remove(path)
                            continue
                        except OSError:
                            pass
                    logger.warning("Cannot use cache directory %s (%s); caching in memory only", directory, e)
                    break
        return self._connect(":memory:")

    @staticmethod
    def _connect(path: str) -> sqlite3.Connection:
        # One event loop uses the connection, but not always the thread that created it.
        db = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        try:
            if path != ":memory:":
                db.execute("PRAGMA journal_mode=WAL")
                db.execute("PRAGMA synchronous=NORMAL")
            if db.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
                db.execute("DROP TABLE IF EXISTS entries")
            db.execute(
                "CREATE TABLE IF NOT EXISTS entries (category TEXT NOT NULL, key TEXT NOT NULL, value BLOB NOT NULL, "
                "size INTEGER NOT NULL, stored_at REAL NOT NULL, expires_at REAL NOT NULL, last_used REAL NOT NULL, "
                "PRIMARY KEY (category, key))"
            )
            db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            db.execute("SELECT count(*) FROM entries").fetchone()  # fails here if the file is damaged
        except BaseException:
            db.close()
            raise
        return db

    def close(self) -> None:
        try:
            self._db.close()
        except sqlite3.Error:
            pass

    # ------------------------------------------------------------------ use

    def get(self, category: str, key: str) -> Hit | None:
        """The cached value, fresh or stale; None if absent or too old even as a fallback."""
        now = self._clock()
        try:
            row = self._db.execute(
                "SELECT value, stored_at, expires_at FROM entries WHERE category = ? AND key = ?", (category, key)
            ).fetchone()
            if row is None:
                return None
            blob, stored_at, expires_at = row
            if now >= expires_at + self.max_stale:
                self._db.execute("DELETE FROM entries WHERE category = ? AND key = ?", (category, key))
                return None
            self._db.execute("UPDATE entries SET last_used = ? WHERE category = ? AND key = ?", (now, category, key))
            return Hit(json.loads(zlib.decompress(blob)), stored_at, fresh=now < expires_at)
        except (sqlite3.Error, zlib.error, ValueError) as e:
            logger.warning("Cache read failed (%s); treating it as a miss", e)
            return None

    def set(self, category: str, key: str, value: Any, ttl: float) -> None:
        """Store a JSON-serialisable value. Nothing is stored if the category is switched off."""
        limit = self.limits.get(category, 0)
        if ttl <= 0 or limit <= 0:
            return
        blob = zlib.compress(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode())
        size = len(blob) + len(key.encode())
        if size > limit:
            return
        now = self._clock()
        try:
            self._db.execute(
                "INSERT OR REPLACE INTO entries VALUES (?, ?, ?, ?, ?, ?, ?)",
                (category, key, blob, size, now, now + ttl, now),
            )
            self._evict(category, limit)
        except sqlite3.Error as e:
            logger.warning("Cache write failed (%s); continuing without storing", e)

    def _evict(self, category: str, limit: int) -> None:
        used = self._db.execute("SELECT COALESCE(SUM(size), 0) FROM entries WHERE category = ?", (category,)).fetchone()[0]
        if used <= limit:
            return
        rows = self._db.execute(
            "SELECT key, size FROM entries WHERE category = ? ORDER BY last_used, stored_at", (category,)
        )
        doomed = []
        for key, size in rows:
            if used <= limit:
                break
            doomed.append((category, key))
            used -= size
        self._db.executemany("DELETE FROM entries WHERE category = ? AND key = ?", doomed)

    def purge(self) -> None:
        """Drop entries that are too old to serve even as a fallback, and enforce the size limits."""
        try:
            self._db.execute("DELETE FROM entries WHERE expires_at + ? <= ?", (self.max_stale, self._clock()))
            for category, limit in self.limits.items():
                self._evict(category, limit)
            self._db.execute("DELETE FROM entries WHERE category NOT IN (%s)" % ",".join("?" * len(CATEGORIES)), CATEGORIES)
        except sqlite3.Error as e:
            logger.warning("Cache clean-up failed (%s)", e)

    def clear(self) -> None:
        self._db.execute("DELETE FROM entries")

    def stats(self) -> dict[str, dict[str, int]]:
        rows = self._db.execute("SELECT category, count(*), COALESCE(SUM(size), 0) FROM entries GROUP BY category")
        found = {c: (n, size) for c, n, size in rows}
        return {c: {"entries": found.get(c, (0, 0))[0], "bytes": found.get(c, (0, 0))[1], "limit_bytes": self.limits[c]}
                for c in CATEGORIES}

    def now(self) -> float:
        return self._clock()


# ----------------------------------------------------- per-call bookkeeping
#
# A tool call may be answered partly or wholly from the cache. The pieces note
# that here, and the tool wrapper adds a "cache" entry to the tool's result.

_served: contextvars.ContextVar[list[tuple[float, bool]] | None] = contextvars.ContextVar("served", default=None)


def start_call() -> contextvars.Token:
    return _served.set([])


def note_served(stored_at: float, stale: bool) -> None:
    served = _served.get()
    if served is not None:
        served.append((stored_at, stale))


def end_call(token: contextvars.Token) -> dict[str, Any] | None:
    """What the finished call took from the cache, or None if everything was fetched just now."""
    served = _served.get() or []
    _served.reset(token)
    if not served:
        return None
    oldest = datetime.fromtimestamp(min(t for t, _ in served), UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    info: dict[str, Any] = {"saved_copy_from": oldest, "stale": any(stale for _, stale in served)}
    if info["stale"]:
        info["warning"] = (
            "The source could not be reached. This answer uses a saved copy that is older than its normal "
            "lifetime and may be outdated."
        )
    return info
