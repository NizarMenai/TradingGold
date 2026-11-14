"""Pluggable high-impact news filter.

Implement NewsSource.events(start, end) to add a source. NewsFilter blocks new signals
from `block_minutes_before` before to `block_minutes_after` after each event.
"""
from __future__ import annotations

import csv
import json
import logging
import time as _time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)
NY = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class NewsEvent:
    time: datetime          # tz-aware UTC
    name: str


class NewsSource(ABC):
    @abstractmethod
    def events(self, start: datetime, end: datetime) -> list[NewsEvent]:
        ...


class StaticCsvNews(NewsSource):
    """CSV with columns date,time_et,event (US/Eastern local time, DST handled)."""

    def __init__(self, path: str | Path, wanted: list[str] | None = None):
        self._events: list[NewsEvent] = []
        wanted_set = {w.upper() for w in wanted} if wanted else None
        with open(path, newline="") as fh:
            rows = [r for r in csv.DictReader(line for line in fh if not line.startswith("#"))]
        for r in rows:
            name = r["event"].strip().upper()
            if wanted_set and name not in wanted_set:
                continue
            local = datetime.fromisoformat(f"{r['date']}T{r['time_et']}").replace(tzinfo=NY)
            self._events.append(NewsEvent(local.astimezone(timezone.utc), name))
        self._events.sort(key=lambda e: e.time)

    def events(self, start: datetime, end: datetime) -> list[NewsEvent]:
        return [e for e in self._events if start <= e.time <= end]

    @property
    def last_event_time(self) -> datetime | None:
        return self._events[-1].time if self._events else None


class ForexFactoryNews(NewsSource):
    """ForexFactory's free weekly JSON export (this week only). Cached on disk and
    refreshed at most every `refresh_hours` to stay polite."""

    KEYWORDS = {"NFP": ["non-farm"], "CPI": ["cpi"], "FOMC": ["fomc", "federal funds rate"]}

    def __init__(self, url: str, cache_file: str | Path, wanted: list[str], refresh_hours: float = 6):
        self.url = url
        self.cache_file = Path(cache_file)
        self.wanted = [w.upper() for w in wanted]
        self.refresh_s = refresh_hours * 3600

    def _load(self) -> list[dict]:
        fresh = self.cache_file.exists() and _time.time() - self.cache_file.stat().st_mtime < self.refresh_s
        if not fresh:
            try:
                import requests
                r = requests.get(self.url, timeout=20, headers={"User-Agent": "goldbot/0.1"})
                r.raise_for_status()
                self.cache_file.write_text(r.text)
            except Exception as exc:  # keep running on the stale copy
                log.warning("ForexFactory calendar refresh failed: %s", exc)
        if not self.cache_file.exists():
            return []
        return json.loads(self.cache_file.read_text())

    def events(self, start: datetime, end: datetime) -> list[NewsEvent]:
        out = []
        for item in self._load():
            if item.get("country") != "USD" or item.get("impact") != "High":
                continue
            title = item.get("title", "").lower()
            for name in self.wanted:
                if any(k in title for k in self.KEYWORDS.get(name, [name.lower()])):
                    t = datetime.fromisoformat(item["date"]).astimezone(timezone.utc)
                    if start <= t <= end:
                        out.append(NewsEvent(t, name))
        return out


class CompositeNews(NewsSource):
    def __init__(self, sources: list[NewsSource]):
        self.sources = sources

    def events(self, start, end):
        seen, out = set(), []
        for s in self.sources:
            for e in s.events(start, end):
                key = (e.time, e.name)
                if key not in seen:
                    seen.add(key)
                    out.append(e)
        return sorted(out, key=lambda e: e.time)


class NewsFilter:
    def __init__(self, source: NewsSource, minutes_before: int = 30, minutes_after: int = 30):
        self.source = source
        self.before = timedelta(minutes=minutes_before)
        self.after = timedelta(minutes=minutes_after)

    def blocking_event(self, ts: datetime) -> NewsEvent | None:
        for e in self.source.events(ts - self.after, ts + self.before):
            return e
        return None

    def mask(self, index: pd.DatetimeIndex) -> np.ndarray:
        """True where trading is allowed (vectorised, for backtests)."""
        ok = np.ones(len(index), dtype=bool)
        if len(index) == 0:
            return ok
        evs = self.source.events(index[0].to_pydatetime() - self.before, index[-1].to_pydatetime() + self.after)
        for e in evs:
            et = pd.Timestamp(e.time)
            ok &= ~((index >= et - self.before) & (index <= et + self.after))
        return ok


def make_news_filter(cfg) -> NewsFilter:
    from .config import resolve_path
    n = cfg.news
    sources: list[NewsSource] = [StaticCsvNews(resolve_path(n.calendar_file), n.events)]
    if "forexfactory" in n.provider:
        sources.append(ForexFactoryNews(n.forexfactory_url, resolve_path("data/ff_calendar_cache.json"),
                                        n.events, n.forexfactory_refresh_hours))
    return NewsFilter(CompositeNews(sources), n.block_minutes_before, n.block_minutes_after)
