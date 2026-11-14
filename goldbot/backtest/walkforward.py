"""Rolling walk-forward validation.

For each fold: pick the best parameter set on the TRAIN window only (by expectancy in R,
requiring a minimum number of trades), then run it untouched on the following TEST
window. The stitched test-window trades are the honest out-of-sample result.
"""
from __future__ import annotations

import itertools
import logging
from dataclasses import dataclass

import pandas as pd

from ..config import with_overrides
from ..strategy import prepare
from . import metrics as M
from .engine import Backtester, BacktestResult

log = logging.getLogger(__name__)


@dataclass
class Fold:
    train_start: pd.Timestamp
    train_end: pd.Timestamp
    test_start: pd.Timestamp
    test_end: pd.Timestamp
    chosen: dict
    train_expectancy_r: float
    train_trades: int
    test_result: BacktestResult


def folds(start, end, train_months: int, test_months: int):
    t0 = pd.Timestamp(start)
    while True:
        tr_end = t0 + pd.DateOffset(months=train_months)
        te_end = tr_end + pd.DateOffset(months=test_months)
        if tr_end >= pd.Timestamp(end):
            return
        yield t0, tr_end, tr_end, min(te_end, pd.Timestamp(end))
        t0 = t0 + pd.DateOffset(months=test_months)


def run_walkforward(cfg, df15, df1h, df4h, news, start, end) -> tuple[list[Fold], pd.DataFrame]:
    wf = cfg.walkforward
    keys = list(wf.grid.keys())
    combos = [dict(zip(keys, vals)) for vals in itertools.product(*[wf.grid[k] for k in keys])]
    # Features depend on the parameters, so build one feature set per combo (causal: safe to
    # compute on the whole history, then restrict trading to each window).
    cache: dict[tuple, tuple] = {}
    for combo in combos:
        c = with_overrides(cfg, "strategy", **combo)
        cache[tuple(combo.values())] = (c, prepare(df15, df1h, df4h, dict(c.strategy)))

    out: list[Fold] = []
    for tr_s, tr_e, te_s, te_e in folds(start, end, wf.train_months, wf.test_months):
        best, best_score, best_n = None, -1e9, 0
        for combo in combos:
            c, feat = cache[tuple(combo.values())]
            res = Backtester(c, feat, news).run(tr_s, tr_e, initial_balance=cfg.backtest.initial_balance)
            tdf = res.trades_df()
            n = len(tdf)
            score = tdf["r_multiple"].mean() if n >= wf.min_train_trades else -1e9
            if score > best_score:
                best, best_score, best_n = combo, score, n
        if best is None or best_score <= -1e9:
            best = {k: cfg.strategy[k] for k in keys}   # not enough trades: fall back to defaults
        c, feat = cache[tuple(best.values())]
        test = Backtester(c, feat, news).run(te_s, te_e, initial_balance=cfg.backtest.initial_balance)
        out.append(Fold(tr_s, tr_e, te_s, te_e, best, best_score, best_n, test))
        log.info("fold test %s..%s chose %s (train %.3fR over %d) -> test %d trades",
                 te_s.date(), te_e.date(), best, best_score, best_n, len(test.trades))
    oos = pd.concat([f.test_result.trades_df() for f in out if f.test_result.trades], ignore_index=True) \
        if any(f.test_result.trades for f in out) else pd.DataFrame()
    return out, oos
