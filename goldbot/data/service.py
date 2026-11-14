"""Keeps the local cache in sync with the provider and serves clean candles.

Cleaning rules:
  * only fully closed candles are stored (open_time + interval <= now)
  * candles stamped while spot gold is closed (weekends, the daily 17:00-18:00 NY break)
    are dropped: some feeds publish flat synthetic bars then
  * gaps (missing bars while the market should be open) are reported, never forward-filled
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pandas as pd

from ..market_hours import expected_bar_times, is_market_open, last_expected_close, open_mask
from .cache import CandleCache
from .provider import INTERVAL_MINUTES, CandleProvider

log = logging.getLogger(__name__)


def clean(df: pd.DataFrame, interval: str, now: datetime | None = None) -> pd.DataFrame:
    if df.empty:
        return df
    minutes = INTERVAL_MINUTES[interval]
    now = now or datetime.now(timezone.utc)
    closed = df.index + pd.Timedelta(minutes=minutes) <= pd.Timestamp(now)
    df = df[closed]
    if minutes <= 60:
        # Drop bars that open while the market is closed. 4h bars can straddle the daily
        # break legitimately, so only drop them on weekends.
        df = df[open_mask(df.index).to_numpy()]
    else:
        # Keep a higher-timeframe bar only if the market was open for at least one 15m slot in it.
        keep = [open_mask(pd.date_range(t, periods=minutes // 15, freq="15min")).any() for t in df.index]
        df = df[keep]
    return df


@dataclass
class GapReport:
    interval: str
    expected: int
    present: int
    missing: int
    largest_gap: pd.Timedelta

    def __str__(self) -> str:
        pct = 100 * self.missing / max(self.expected, 1)
        return (f"{self.interval}: {self.present}/{self.expected} expected bars present, "
                f"{self.missing} missing ({pct:.2f}%), largest gap {self.largest_gap}")


class DataService:
    def __init__(self, provider: CandleProvider, cache: CandleCache, symbol: str):
        self.provider = provider
        self.cache = cache
        self.symbol = symbol

    def _fetch_forward(self, interval: str, start: pd.Timestamp, end: pd.Timestamp) -> int:
        minutes = INTERVAL_MINUTES[interval]
        step = pd.Timedelta(minutes=minutes)
        cursor, total = start, 0
        while cursor < end:
            # Twelve Data returns the LATEST N bars of an over-wide range, so page with
            # windows that can never hold more than one request's worth of bars.
            win_end = min(end, cursor + step * (self.provider.max_bars_per_request - 1))
            df = self.provider.fetch(self.symbol, interval, start=cursor.to_pydatetime(), end=win_end.to_pydatetime())
            df = clean(df, interval)
            if df.empty:
                cursor = win_end + step   # holiday, or before the provider's history starts
                continue
            total += self.cache.upsert(self.symbol, interval, df)
            cursor = max(df.index[-1], win_end) + step
            log.info("%s: cached up to %s (%d bars this run)", interval, df.index[-1], total)
        return total

    def sync(self, interval: str, start: datetime | None = None, end: datetime | None = None) -> int:
        """Ensure [start, end] is cached. Only fetches what is missing at either end."""
        end_ts = pd.Timestamp(end or datetime.now(timezone.utc))
        lo, hi = self.cache.bounds(self.symbol, interval)
        n = 0
        if start is not None:
            start_ts = pd.Timestamp(start)
            if lo is None or start_ts < lo:
                n += self._fetch_forward(interval, start_ts, lo if lo is not None else end_ts)
        lo, hi = self.cache.bounds(self.symbol, interval)
        if hi is None:
            if start is None:
                raise ValueError("empty cache: give a start date for the first sync")
        elif hi < end_ts:
            n += self._fetch_forward(interval, hi, end_ts)
        return n

    def refresh_latest(self, interval: str, bars: int = 10) -> int:
        """Cheap live update: one request for the last few bars."""
        df = clean(self.provider.fetch(self.symbol, interval, outputsize=bars), interval)
        return self.cache.upsert(self.symbol, interval, df)

    def candles(self, interval: str, start: datetime | None = None, end: datetime | None = None) -> pd.DataFrame:
        return self.cache.load(self.symbol, interval, start, end)

    def gap_report(self, interval: str, start: datetime, end: datetime) -> GapReport:
        minutes = INTERVAL_MINUTES[interval]
        have = self.candles(interval, start, end).index
        if minutes > 60:
            return GapReport(interval, len(have), len(have), 0, pd.Timedelta(0))
        exp = expected_bar_times(start, end, minutes)
        missing = exp.difference(have)
        diffs = have.to_series().diff()
        largest = diffs.max() if len(diffs) > 1 else pd.Timedelta(0)
        return GapReport(interval, len(exp), len(exp.intersection(have)), len(missing), largest)

    def staleness(self, interval: str = "15min", now: datetime | None = None) -> timedelta | None:
        """How far the newest cached bar lags the newest bar that should exist. None if market closed."""
        now = now or datetime.now(timezone.utc)
        if not is_market_open(now):
            return None
        _, hi = self.cache.bounds(self.symbol, interval)
        if hi is None:
            return timedelta.max
        newest_close = hi + pd.Timedelta(minutes=INTERVAL_MINUTES[interval])
        return max(timedelta(0), last_expected_close(now, INTERVAL_MINUTES[interval]) - newest_close.to_pydatetime())
