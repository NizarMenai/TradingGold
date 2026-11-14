"""Provider-agnostic interface for OHLC candle sources.

To add a new source, subclass CandleProvider, implement fetch(), and register it in
goldbot/data/providers.py. Everything else (cache, strategy, backtest) only sees
DataFrames in the canonical format below.

Canonical candle frame:
  index   : DatetimeIndex, tz-aware UTC, the candle OPEN time, ascending, unique
  columns : open, high, low, close (float64)
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

import pandas as pd

INTERVAL_MINUTES = {"15min": 15, "1h": 60, "4h": 240}
COLUMNS = ["open", "high", "low", "close"]


class ProviderError(RuntimeError):
    pass


class RateLimitError(ProviderError):
    pass


class CandleProvider(ABC):
    name: str = "abstract"
    max_bars_per_request: int = 5000

    @abstractmethod
    def fetch(self, symbol: str, interval: str, start: datetime | None = None,
              end: datetime | None = None, outputsize: int | None = None) -> pd.DataFrame:
        """Return candles in the canonical format. start/end are tz-aware UTC and inclusive."""


def empty_frame() -> pd.DataFrame:
    return pd.DataFrame(columns=COLUMNS, index=pd.DatetimeIndex([], tz="UTC", name="time"), dtype="float64")


def normalize(df: pd.DataFrame) -> pd.DataFrame:
    """Coerce any provider output to the canonical format."""
    if df.empty:
        return empty_frame()
    df = df[COLUMNS].astype("float64")
    idx = pd.DatetimeIndex(df.index)
    idx = idx.tz_localize("UTC") if idx.tz is None else idx.tz_convert("UTC")
    df.index = idx.rename("time")
    df = df[~df.index.duplicated(keep="last")].sort_index()
    bad = (df["high"] < df[["open", "close", "low"]].max(axis=1)) | (df["low"] > df[["open", "close", "high"]].min(axis=1))
    return df[~bad]
