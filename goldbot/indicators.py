"""Plain, explainable technical indicators (pandas only). All are causal: the value at
bar i uses data up to and including bar i, never later bars."""
from __future__ import annotations

import numpy as np
import pandas as pd


def ema(s: pd.Series, period: int) -> pd.Series:
    """Exponential moving average, alpha = 2/(period+1), seeded with the SMA of the first `period` values."""
    s = s.astype("float64")
    out = pd.Series(np.nan, index=s.index)
    if len(s) < period:
        return out
    alpha = 2.0 / (period + 1)
    vals = s.to_numpy()
    res = np.full(len(vals), np.nan)
    res[period - 1] = vals[:period].mean()
    for i in range(period, len(vals)):
        res[i] = alpha * vals[i] + (1 - alpha) * res[i - 1]
    return pd.Series(res, index=s.index)


def _wilder(s: pd.Series, period: int) -> pd.Series:
    """Wilder smoothing (RMA): seeded with an SMA, then x_t = (x_{t-1}*(n-1) + v_t)/n."""
    vals = s.to_numpy(dtype="float64")
    res = np.full(len(vals), np.nan)
    valid = np.where(~np.isnan(vals))[0]
    if len(valid) < period:
        return pd.Series(res, index=s.index)
    first = valid[0]
    seed_end = first + period
    res[seed_end - 1] = vals[first:seed_end].mean()
    for i in range(seed_end, len(vals)):
        res[i] = (res[i - 1] * (period - 1) + vals[i]) / period
    return pd.Series(res, index=s.index)


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """Wilder's RSI in [0, 100]."""
    delta = close.astype("float64").diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = _wilder(gain, period)
    avg_loss = _wilder(loss, period)
    with np.errstate(divide="ignore", invalid="ignore"):
        rs = avg_gain / avg_loss
        out = 100 - 100 / (1 + rs)
    out[(avg_loss == 0) & (avg_gain > 0)] = 100.0
    out[(avg_loss == 0) & (avg_gain == 0)] = 50.0
    return out


def true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["close"].shift(1)
    tr = pd.concat([df["high"] - df["low"], (df["high"] - prev_close).abs(), (df["low"] - prev_close).abs()], axis=1)
    return tr.max(axis=1, skipna=False).fillna(df["high"] - df["low"])


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Wilder's Average True Range."""
    return _wilder(true_range(df), period)


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    line = ema(close, fast) - ema(close, slow)
    sig = ema(line.dropna(), signal).reindex(close.index)
    return pd.DataFrame({"macd": line, "signal": sig, "hist": line - sig})


def swing_highs(high: pd.Series, n: int = 2) -> pd.Series:
    """Fractal swing high: high[i] is strictly the max of the 2n+1 bars centred on i.
    NOTE: only *confirmed* n bars later. The value is placed at bar i+n (the bar on
    which it becomes known) to stay causal. Non-swing bars are NaN."""
    h = high.to_numpy(dtype="float64")
    out = np.full(len(h), np.nan)
    for i in range(n, len(h) - n):
        window = h[i - n:i + n + 1]
        if h[i] == window.max() and (window == h[i]).sum() == 1:
            out[i + n] = h[i]
    return pd.Series(out, index=high.index)


def swing_lows(low: pd.Series, n: int = 2) -> pd.Series:
    """Fractal swing low, confirmed and placed n bars later (see swing_highs)."""
    return -swing_highs(-low, n)
