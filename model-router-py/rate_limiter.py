"""
Token-aware sliding-window rate limiter in on-disk SQLite.

One row per admitted request (tenant, ts, tokens); a tenant's usage is the sum
of its rows from the last 60 s.
"""

import sqlite3
import threading
import time

WINDOW_SECONDS = 60


class RateLimiter:
    def __init__(self, path: str, limit: int) -> None:
        self.limit = limit
        # The lock serializes threads on the shared connection; BEGIN IMMEDIATE does so across processes.
        self.lock = threading.Lock()
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None, timeout=5)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS token_usage ("
            "id INTEGER PRIMARY KEY, tenant TEXT NOT NULL, ts REAL NOT NULL, tokens INTEGER NOT NULL)"
        )
        self.db.execute("CREATE INDEX IF NOT EXISTS token_usage_tenant_ts ON token_usage (tenant, ts)")

    def reserve(self, tenant: str, tokens: int) -> tuple[int | None, float]:
        """Charge `tokens` if they fit. Returns (usage_id, 0), or (None, seconds until they fit)."""
        now = time.time()
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                # Evict expired rows.
                self.db.execute("DELETE FROM token_usage WHERE tenant = ? AND ts <= ?",
                                (tenant, now - WINDOW_SECONDS))
                used = self.db.execute("SELECT COALESCE(SUM(tokens), 0) FROM token_usage WHERE tenant = ?",
                                       (tenant,)).fetchone()[0]
                if used + tokens > self.limit:
                    retry_after = self._retry_after(tenant, used + tokens - self.limit, now)
                    self.db.execute("COMMIT")
                    return None, retry_after
                usage_id = self.db.execute("INSERT INTO token_usage (tenant, ts, tokens) VALUES (?, ?, ?)",
                                           (tenant, now, tokens)).lastrowid
                self.db.execute("COMMIT")
                return usage_id, 0
            except BaseException:
                self.db.execute("ROLLBACK")
                raise

    def settle(self, usage_id: int, tokens: int) -> None:
        """Replace a reservation with actual usage."""
        with self.lock:
            self.db.execute("UPDATE token_usage SET tokens = ? WHERE id = ?", (tokens, usage_id))

    def _retry_after(self, tenant: str, excess: int, now: float) -> float:
        """Seconds until enough of the oldest rows expire to free `excess` tokens."""
        freed = 0
        for ts, tokens in self.db.execute("SELECT ts, tokens FROM token_usage WHERE tenant = ? ORDER BY ts",
                                          (tenant,)):
            freed += tokens
            if freed >= excess:
                return ts + WINDOW_SECONDS - now
        return WINDOW_SECONDS
