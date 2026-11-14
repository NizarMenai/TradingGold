"""Rule-based trend-pullback strategy for XAU/USD. Every rule is listed in `explain()`.

Long setup (shorts are the exact mirror):
 1. Trend    : on BOTH the last closed 4h and 1h bars, EMA50 > EMA200 (and close > EMA50).
 2. Pullback : within the last `pullback_bars` 15m bars, price came within
               `pullback_touch_atr` x ATR of the 15m EMA21, and momentum cooled
               (RSI dipped to <= rsi_pullback_level, or MACD histogram went negative).
 3. Trigger  : the signal bar closes back above EMA21 as a bullish candle while momentum
               turns back up (RSI crosses above rsi_trigger_level, or MACD histogram crosses above 0).
 4. Structure: the pullback low (lowest low of the last `swing_lookback` bars) is a higher low
               versus the swing low before it, so the up-structure is intact.
 5. Stop     : the wider of (entry - sl_atr_mult x ATR) and (pullback low - structure_buffer_atr x ATR).
               If that is wider than max_sl_atr_mult x ATR, the setup is skipped.
 6. Targets  : TP1 = entry + tp1_r x R, TP2 = entry + tp2_r x R  (R = entry - stop).
 7. Filters  : the signal bar must close inside a London/New York window and outside news blackouts.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

import numpy as np
import pandas as pd

from .indicators import atr, ema, macd, rsi
from .news import NewsFilter
from .sessions import SessionFilter

BAR = pd.Timedelta(minutes=15)


@dataclass
class Signal:
    time: datetime                 # signal bar CLOSE time (UTC): when the signal is known
    direction: str                 # "BUY" or "SELL"
    entry: float
    sl: float
    tp1: float
    tp2: float
    atr: float
    reason: str
    session: str = ""
    meta: dict = field(default_factory=dict)

    @property
    def risk_per_oz(self) -> float:
        return abs(self.entry - self.sl)

    @property
    def id(self) -> str:
        return f"{self.time:%Y%m%dT%H%M}-{self.direction}"


def _htf_trend(df: pd.DataFrame, minutes: int, p, index_close: pd.DatetimeIndex) -> pd.Series:
    """+1/-1/0 trend of the last CLOSED higher-timeframe bar, aligned to 15m close times."""
    fast, slow = ema(df["close"], p["trend_ema_fast"]), ema(df["close"], p["trend_ema_slow"])
    up = fast > slow
    dn = fast < slow
    if p["require_price_above_fast"]:
        up &= df["close"] > fast
        dn &= df["close"] < fast
    trend = pd.Series(np.where(up, 1, np.where(dn, -1, 0)), index=df.index)
    trend[fast.isna() | slow.isna()] = 0
    # Re-index on the HTF bar's close time so a 15m bar only sees HTF bars that had closed.
    trend.index = trend.index + pd.Timedelta(minutes=minutes)
    left = pd.DataFrame({"t": index_close})
    right = pd.DataFrame({"t": trend.index, "trend": trend.to_numpy()})
    merged = pd.merge_asof(left, right, on="t", direction="backward")
    return pd.Series(merged["trend"].fillna(0).astype(int).to_numpy(), index=index_close)


def prepare(df15: pd.DataFrame, df1h: pd.DataFrame, df4h: pd.DataFrame, p: dict) -> pd.DataFrame:
    """Compute every feature the rules need, one row per 15m bar. Index = bar CLOSE time."""
    f = df15.copy()
    f.index = f.index + BAR                     # decisions happen at the close
    f["ema"] = ema(df15["close"], p["entry_ema"]).to_numpy()
    f["atr"] = atr(df15, p["atr_period"]).to_numpy()
    f["rsi"] = rsi(df15["close"], p["rsi_period"]).to_numpy()
    m = macd(df15["close"], p["macd_fast"], p["macd_slow"], p["macd_signal"])
    f["hist"] = m["hist"].to_numpy()
    f["trend4h"] = _htf_trend(df4h, 240, p, f.index).to_numpy()
    f["trend1h"] = _htf_trend(df1h, 60, p, f.index).to_numpy()
    f["trend"] = np.where((f["trend4h"] == 1) & (f["trend1h"] == 1), 1,
                          np.where((f["trend4h"] == -1) & (f["trend1h"] == -1), -1, 0))

    pb, L = p["pullback_bars"], p["swing_lookback"]
    touch = p["pullback_touch_atr"] * f["atr"]
    f["pb_long"] = ((f["low"] - (f["ema"] + touch)).rolling(pb).min() <= 0)
    f["pb_short"] = ((f["high"] - (f["ema"] - touch)).rolling(pb).max() >= 0)

    if p["momentum"] == "rsi":
        lvl, trig = p["rsi_pullback_level"], p["rsi_trigger_level"]
        f["mom_long"] = (f["rsi"].rolling(pb).min() <= lvl) & (f["rsi"] > trig) & (f["rsi"].shift(1) <= trig)
        f["mom_short"] = (f["rsi"].rolling(pb).max() >= 100 - lvl) & (f["rsi"] < 100 - trig) & (f["rsi"].shift(1) >= 100 - trig)
    else:
        f["mom_long"] = (f["hist"].rolling(pb).min() < 0) & (f["hist"] > 0) & (f["hist"].shift(1) <= 0)
        f["mom_short"] = (f["hist"].rolling(pb).max() > 0) & (f["hist"] < 0) & (f["hist"].shift(1) >= 0)

    f["trig_long"] = (f["close"] > f["ema"]) & (f["close"] > f["open"])
    f["trig_short"] = (f["close"] < f["ema"]) & (f["close"] < f["open"])

    f["swing_low"] = f["low"].rolling(L).min()
    f["swing_high"] = f["high"].rolling(L).max()
    prev_low = f["low"].shift(L).rolling(2 * L).min()
    prev_high = f["high"].shift(L).rolling(2 * L).max()
    if p["require_higher_low"]:
        f["struct_long"] = f["swing_low"] > prev_low
        f["struct_short"] = f["swing_high"] < prev_high
    else:
        f["struct_long"] = f["struct_short"] = True

    f["setup_long"] = (f["trend"] == 1) & f["pb_long"] & f["mom_long"] & f["trig_long"] & f["struct_long"]
    f["setup_short"] = (f["trend"] == -1) & f["pb_short"] & f["mom_short"] & f["trig_short"] & f["struct_short"]
    return f


def build_signal(row: pd.Series, ts: pd.Timestamp, p: dict) -> tuple[Signal | None, str]:
    """Turn a setup row into a concrete signal with levels. Returns (signal, skip_reason)."""
    if row["setup_long"]:
        direction, sgn, extreme = "BUY", 1, row["swing_low"]
    elif row["setup_short"]:
        direction, sgn, extreme = "SELL", -1, row["swing_high"]
    else:
        return None, "no setup"
    a, entry = float(row["atr"]), float(row["close"])
    if not np.isfinite(a) or a <= 0:
        return None, "ATR unavailable"
    atr_stop = entry - sgn * p["sl_atr_mult"] * a
    struct_stop = extreme - sgn * p["structure_buffer_atr"] * a
    sl = min(atr_stop, struct_stop) if sgn == 1 else max(atr_stop, struct_stop)
    risk = abs(entry - sl)
    if risk > p["max_sl_atr_mult"] * a:
        return None, f"stop {risk / a:.1f} ATR wider than max {p['max_sl_atr_mult']}"
    tp1 = entry + sgn * p["tp1_r"] * risk
    tp2 = entry + sgn * p["tp2_r"] * risk
    mom = (f"RSI {row['rsi']:.0f} turned {'up through' if sgn == 1 else 'down through'} "
           f"{p['rsi_trigger_level'] if sgn == 1 else 100 - p['rsi_trigger_level']}") if p["momentum"] == "rsi" \
        else f"MACD hist crossed {'above' if sgn == 1 else 'below'} 0"
    reason = (f"4h+1h {'up' if sgn == 1 else 'down'}trend (EMA{p['trend_ema_fast']}/{p['trend_ema_slow']}), "
              f"15m pullback to EMA{p['entry_ema']}, {mom}, "
              f"{'higher low' if sgn == 1 else 'lower high'} at {extreme:.2f}")
    return Signal(ts.to_pydatetime(), direction, round(entry, 2), round(sl, 2), round(tp1, 2), round(tp2, 2),
                  round(a, 2), reason), ""


def signal_candidates(features: pd.DataFrame, p: dict, sessions: SessionFilter,
                      news: NewsFilter | None) -> pd.DataFrame:
    """Rows where a setup fired AND session/news filters allow it."""
    allowed = sessions.mask(features.index)
    if news is not None:
        allowed &= news.mask(features.index)
    setups = (features["setup_long"] | features["setup_short"]).to_numpy()
    return features[setups & allowed]
