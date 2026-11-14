"""Performance statistics. R-multiples make results comparable across account sizes."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass
class Metrics:
    trades: int
    wins: int
    win_rate: float
    profit_factor: float
    expectancy_usd: float
    expectancy_r: float
    avg_win_r: float
    avg_loss_r: float
    total_r: float
    net_pnl: float
    return_pct: float
    max_drawdown_pct: float
    max_drawdown_usd: float
    longest_losing_streak: int
    trades_per_month: float
    final_balance: float

    def as_rows(self) -> list[tuple[str, str]]:
        pf = "inf" if np.isinf(self.profit_factor) else f"{self.profit_factor:.2f}"
        return [
            ("Trades", f"{self.trades}"),
            ("Trades per month", f"{self.trades_per_month:.1f}"),
            ("Win rate", f"{100 * self.win_rate:.1f}%"),
            ("Profit factor", pf),
            ("Expectancy per trade", f"{self.expectancy_r:+.3f} R  (${self.expectancy_usd:+.3f})"),
            ("Average win / loss", f"{self.avg_win_r:+.2f} R / {self.avg_loss_r:+.2f} R"),
            ("Total", f"{self.total_r:+.1f} R"),
            ("Net P&L", f"${self.net_pnl:+.2f} ({self.return_pct:+.1f}%)"),
            ("Max drawdown", f"{self.max_drawdown_pct:.1f}% (${self.max_drawdown_usd:.2f})"),
            ("Longest losing streak", f"{self.longest_losing_streak}"),
            ("Final balance", f"${self.final_balance:.2f}"),
        ]


def longest_streak(flags: list[bool]) -> int:
    best = cur = 0
    for f in flags:
        cur = cur + 1 if f else 0
        best = max(best, cur)
    return best


def max_drawdown(equity: pd.Series) -> tuple[float, float]:
    if equity.empty:
        return 0.0, 0.0
    peak = equity.cummax()
    dd = equity - peak
    pct = (dd / peak).min()
    return float(-100 * pct), float(-dd.min())


def compute(trades: pd.DataFrame, equity: pd.Series, initial: float, start, end) -> Metrics:
    months = max((pd.Timestamp(end) - pd.Timestamp(start)).days / 30.44, 1e-9)
    if trades.empty:
        return Metrics(0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, initial)
    pnl = trades["pnl_usd"].to_numpy()
    r = trades["r_multiple"].to_numpy()
    wins = pnl > 0
    gross_win, gross_loss = pnl[wins].sum(), -pnl[~wins].sum()
    pf = gross_win / gross_loss if gross_loss > 0 else float("inf")
    dd_pct, dd_usd = max_drawdown(equity)
    final = float(equity.iloc[-1])
    return Metrics(
        trades=len(pnl), wins=int(wins.sum()), win_rate=float(wins.mean()), profit_factor=float(pf),
        expectancy_usd=float(pnl.mean()), expectancy_r=float(r.mean()),
        avg_win_r=float(r[wins].mean()) if wins.any() else 0.0,
        avg_loss_r=float(r[~wins].mean()) if (~wins).any() else 0.0,
        total_r=float(r.sum()), net_pnl=float(final - initial), return_pct=float(100 * (final / initial - 1)),
        max_drawdown_pct=dd_pct, max_drawdown_usd=dd_usd,
        longest_losing_streak=longest_streak(list(~wins)), trades_per_month=len(pnl) / months, final_balance=final)


def r_equity(trades: pd.DataFrame, initial: float = 50.0, risk_pct: float = 1.0) -> pd.Series:
    """Fixed-fractional equity path implied by the R-multiples alone (no lot rounding)."""
    if trades.empty:
        return pd.Series(dtype=float)
    growth = 1 + trades["r_multiple"].to_numpy() * risk_pct / 100
    return pd.Series(initial * np.cumprod(growth), index=pd.DatetimeIndex(trades["exit_time"]))


def bootstrap_expectancy_ci(r: np.ndarray, n: int = 5000, seed: int = 7) -> tuple[float, float]:
    """95% bootstrap confidence interval for mean R. Tells you whether the edge could be luck."""
    if len(r) < 5:
        return float("nan"), float("nan")
    rng = np.random.default_rng(seed)
    means = rng.choice(r, size=(n, len(r)), replace=True).mean(axis=1)
    return float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5))
