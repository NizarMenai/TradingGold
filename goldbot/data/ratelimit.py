"""Rate limiter that enforces per-minute and per-day request budgets.

The daily counter is persisted in SQLite so restarts do not reset it.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from collections import deque
from datetime import datetime, timezone

from .provider import RateLimitError


class RateLimiter:
    def __init__(self, per_minute: int, per_day: int, db_path: str | None = None,
                 clock=time.monotonic, sleep=time.sleep):
        self.per_minute = per_minute
        self.per_day = per_day
        self._clock = clock
        self._sleep = sleep
        self._recent: deque[float] = deque()
        self._lock = threading.Lock()
        self._db_path = db_path
        if db_path:
            with sqlite3.connect(db_path) as con:
                con.execute("CREATE TABLE IF NOT EXISTS api_usage (day TEXT PRIMARY KEY, n INTEGER NOT NULL)")

    def _today(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def used_today(self) -> int:
        if not self._db_path:
            return 0
        with sqlite3.connect(self._db_path) as con:
            row = con.execute("SELECT n FROM api_usage WHERE day=?", (self._today(),)).fetchone()
        return row[0] if row else 0

    def _record(self) -> None:
        if not self._db_path:
            return
        with sqlite3.connect(self._db_path) as con:
            con.execute("INSERT INTO api_usage(day, n) VALUES(?, 1) "
                        "ON CONFLICT(day) DO UPDATE SET n = n + 1", (self._today(),))

    def acquire(self) -> None:
        """Block until a request is allowed; raise if the daily budget is spent."""
        with self._lock:
            if self.used_today() >= self.per_day:
                raise RateLimitError(f"daily request budget of {self.per_day} reached")
            while True:
                now = self._clock()
                while self._recent and now - self._recent[0] >= 60.0:
                    self._recent.popleft()
                if len(self._recent) < self.per_minute:
                    break
                self._sleep(60.0 - (now - self._recent[0]) + 0.05)
            self._recent.append(self._clock())
            self._record()
