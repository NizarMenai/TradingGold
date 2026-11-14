import numpy as np
import pandas as pd
import pytest

from goldbot.indicators import atr, ema, macd, rsi, swing_highs, swing_lows, true_range
from tests.conftest import make_ohlc


def test_ema_seed_and_recursion():
    s = pd.Series([1, 2, 3, 4, 5, 6], dtype=float)
    e = ema(s, 3)
    assert e.iloc[:2].isna().all()
    assert e.iloc[2] == pytest.approx(2.0)              # SMA seed of 1,2,3
    assert e.iloc[3] == pytest.approx(0.5 * 4 + 0.5 * 2.0)
    assert e.iloc[5] == pytest.approx(0.5 * 6 + 0.5 * (0.5 * 5 + 0.5 * 3.0))


def test_ema_constant_series():
    assert np.allclose(ema(pd.Series([7.0] * 50), 10).dropna(), 7.0)


def test_ema_is_causal():
    s = pd.Series(np.random.default_rng(1).normal(size=300).cumsum())
    full = ema(s, 20)
    part = ema(s.iloc[:200], 20)
    pd.testing.assert_series_equal(full.iloc[:200], part)


def test_rsi_extremes_and_flat():
    up = pd.Series(np.arange(1, 40, dtype=float))
    assert rsi(up, 14).dropna().eq(100).all()
    down = pd.Series(np.arange(40, 1, -1, dtype=float))
    assert rsi(down, 14).dropna().eq(0).all()
    flat = pd.Series([5.0] * 30)
    assert rsi(flat, 14).dropna().eq(50).all()


def test_rsi_matches_manual_wilder():
    rng = np.random.default_rng(3)
    s = pd.Series(100 + rng.normal(size=60).cumsum())
    n = 14
    d = s.diff().to_numpy()
    g, l = np.clip(d, 0, None), np.clip(-d, 0, None)
    ag, al = g[1:n + 1].mean(), l[1:n + 1].mean()
    expected = [100 - 100 / (1 + ag / al)]
    for i in range(n + 1, len(s)):
        ag = (ag * (n - 1) + g[i]) / n
        al = (al * (n - 1) + l[i]) / n
        expected.append(100 - 100 / (1 + ag / al))
    np.testing.assert_allclose(rsi(s, n).iloc[n:].to_numpy(), expected, rtol=1e-10)


def test_rsi_bounded():
    s = pd.Series(np.random.default_rng(5).normal(size=500).cumsum() + 1000)
    r = rsi(s).dropna()
    assert ((r >= 0) & (r <= 100)).all()


def test_true_range_uses_previous_close():
    df = pd.DataFrame({"open": [10, 15], "high": [11, 16], "low": [9, 14], "close": [10, 15]}, dtype=float)
    tr = true_range(df)
    assert tr.iloc[0] == 2          # first bar: high - low
    assert tr.iloc[1] == 6          # gap: |16 - 10|


def test_atr_constant_range():
    df = make_ohlc([100.0] * 40, spread=2.0)
    a = atr(df, 14).dropna()
    assert np.allclose(a, 2.0)


def test_macd_constant_is_zero():
    m = macd(pd.Series([50.0] * 100))
    assert np.allclose(m.dropna().to_numpy(), 0.0)


def test_macd_positive_in_uptrend():
    m = macd(pd.Series(np.linspace(100, 200, 200)))
    assert (m["macd"].dropna() > 0).all()


def test_swings_confirmed_late_and_causal():
    high = pd.Series([1, 2, 5, 2, 1, 1, 1], dtype=float)
    sh = swing_highs(high, n=2)
    assert np.isnan(sh.iloc[2])               # not known on the peak bar itself
    assert sh.iloc[4] == 5                    # known 2 bars later
    low = pd.Series([5, 4, 1, 4, 5, 5], dtype=float)
    assert swing_lows(low, n=2).iloc[4] == 1


def test_swing_requires_unique_max():
    high = pd.Series([1, 5, 5, 1, 1, 1], dtype=float)
    assert swing_highs(high, n=1).isna().all()
