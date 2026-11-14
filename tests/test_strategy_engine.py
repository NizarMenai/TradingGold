"""Strategy alignment and backtest execution rules on hand-made data."""
import numpy as np
import pandas as pd
import pytest

from goldbot.backtest.engine import Backtester
from goldbot.config import with_overrides
from goldbot.market_hours import is_market_open
from goldbot.sessions import SessionFilter
from goldbot.strategy import _htf_trend, build_signal


def _features(n=40, start="2025-03-04 09:00", price=2000.0):
    idx = pd.date_range(start, periods=n, freq="15min", tz="UTC")     # bar CLOSE times
    f = pd.DataFrame({"open": price, "high": price + 1, "low": price - 1, "close": price}, index=idx)
    f["atr"] = 4.0
    f["rsi"] = 55.0
    f["swing_low"] = price - 3
    f["swing_high"] = price + 3
    f["setup_long"] = False
    f["setup_short"] = False
    return f


def _cfg(cfg, **costs):
    c = with_overrides(cfg, "costs", spread=0.0, slippage_entry=0.0, slippage_stop=0.0,
                       swap_long_annual_pct=0.0, swap_short_annual_pct=0.0)
    c = with_overrides(c, "costs", **costs) if costs else c
    return with_overrides(c, "account", type="cent")


def test_htf_trend_uses_only_closed_bars(cfg):
    p = dict(cfg.strategy, trend_ema_fast=2, trend_ema_slow=3, require_price_above_fast=False)
    idx4 = pd.date_range("2025-03-03 00:00", periods=6, freq="4h", tz="UTC")
    df4 = pd.DataFrame({"close": [10, 9, 8, 7, 20, 30.0]}, index=idx4)   # turns up in the 04:00-08:00 Tue bar
    df4["open"] = df4["high"] = df4["low"] = df4["close"]
    closes = pd.date_range("2025-03-03 16:00", "2025-03-04 00:00", freq="15min", tz="UTC")
    tr = _htf_trend(df4, 240, p, closes)
    # Bar opened 16:00 (close 20:00) is the first up bar. It must not be visible before 20:00.
    assert (tr[tr.index < pd.Timestamp("2025-03-03 20:00", tz="UTC")] <= 0).all()
    assert tr[pd.Timestamp("2025-03-03 20:00", tz="UTC")] == 1


def test_build_signal_levels(cfg):
    p = dict(cfg.strategy)
    row = _features().iloc[10].copy()
    row["setup_long"] = True
    sig, _ = build_signal(row, row.name, p)
    # ATR stop 2000 - 1.5*4 = 1994; structure stop 1997 - 0.2*4 = 1996.2 -> wider (1994) wins
    assert sig.direction == "BUY" and sig.sl == 1994.0
    assert sig.tp1 == 2012.0 and sig.tp2 == 2018.0
    assert (sig.tp1 - sig.entry) / (sig.entry - sig.sl) >= 2.0


def test_build_signal_skips_too_wide_structure(cfg):
    p = dict(cfg.strategy)
    row = _features().iloc[10].copy()
    row["setup_short"] = True
    row["swing_high"] = 2020.0            # structure stop would be > 3 ATR away
    sig, why = build_signal(row, row.name, p)
    assert sig is None and "wider" in why


def _run(cfg, f, **kw):
    c = _cfg(cfg, **kw)
    bt = Backtester(c, f, news=None)
    return bt.run(f.index[0], f.index[-1], initial_balance=50.0)


def test_stop_assumed_first_when_bar_hits_both(cfg):
    f = _features()
    f.iloc[5, f.columns.get_loc("setup_long")] = True           # signal at close of bar 5
    f.iloc[7, f.columns.get_loc("high")] = 2030.0                 # bar 7 spans SL and TP
    f.iloc[7, f.columns.get_loc("low")] = 1980.0
    res = _run(cfg, f)
    assert len(res.trades) == 1
    t = res.trades[0]
    assert t.exit_reason == "SL" and t.pnl_usd < 0
    assert t.r_multiple == pytest.approx(-1.0)


def test_tp1_then_breakeven(cfg):
    f = _features()
    f.iloc[5, f.columns.get_loc("setup_long")] = True
    f.iloc[8, f.columns.get_loc("high")] = 2013.0                 # TP1 (2012) hit
    f.iloc[10, f.columns.get_loc("low")] = 1999.0                 # back to entry -> BE stop
    res = _run(cfg, f)
    t = res.trades[0]
    assert t.tp1_hit and t.exit_reason == "breakeven after TP1"
    # half closed at +2R, half at 0 -> +1R
    assert t.r_multiple == pytest.approx(1.0, abs=0.05)


def test_costs_reduce_pnl(cfg):
    f = _features()
    f.iloc[5, f.columns.get_loc("setup_long")] = True
    f.iloc[8, f.columns.get_loc("high")] = 2025.0                 # TP1 and TP2 both hit
    free = _run(cfg, f).trades[0]
    costly = _run(cfg, f, spread=0.5, slippage_entry=0.2).trades[0]
    assert free.exit_reason == costly.exit_reason == "TP2"
    assert costly.pnl_usd < free.pnl_usd


def test_signals_outside_session_are_ignored(cfg):
    f = _features(start="2025-03-04 01:00")                        # Asian session
    f.iloc[5, f.columns.get_loc("setup_long")] = True
    assert _run(cfg, f).trades == []


def test_session_filter_dst():
    sf = SessionFilter([{"name": "NY", "tz": "America/New_York", "start": "08:00", "end": "16:00"}])
    assert sf.session_at(pd.Timestamp("2025-07-01 12:00", tz="UTC")) == "NY"     # 08:00 EDT
    assert sf.session_at(pd.Timestamp("2025-01-07 12:00", tz="UTC")) is None     # 07:00 EST
    assert sf.session_at(pd.Timestamp("2025-01-07 13:00", tz="UTC")) == "NY"


def test_market_hours():
    assert not is_market_open(pd.Timestamp("2025-03-08 12:00", tz="UTC"))          # Saturday
    assert not is_market_open(pd.Timestamp("2025-03-07 22:30", tz="UTC"))          # Fri after 17:00 NY
    assert is_market_open(pd.Timestamp("2025-03-09 23:30", tz="UTC"))              # Sun after 18:00 NY (EDT)
    assert not is_market_open(pd.Timestamp("2025-03-11 21:30", tz="UTC"))          # daily break 17:30 NY
