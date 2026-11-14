"""Event-driven bar-by-bar backtester that applies the same rules the live bot uses.

Execution model (deliberately conservative):
  * A signal is known at the 15m bar close; entry is a market order at the NEXT bar's
    open, paying half the spread plus entry slippage. If that bar is not contiguous
    (weekend/gap) or price already passed SL or TP1, the signal is skipped.
  * Prices are treated as mid. Longs exit on the bid (mid - spread/2), shorts on the ask.
  * If a bar touches both the stop and a target, the STOP is assumed to fill first.
  * Stops fill at the stop price minus stop slippage, or at the open if price gapped through.
  * Take-profits are limit orders: filled at the target, no slippage.
  * Swap is charged for each 17:00 New York rollover held (triple on Wednesdays).
  * Positions are closed at market after max_hold_hours and before the weekend.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from ..news import NewsFilter
from ..risk import AccountType, RiskManager, size_position, split_lots
from ..sessions import SessionFilter
from ..strategy import Signal, build_signal, signal_candidates

NY = ZoneInfo("America/New_York")


@dataclass
class Trade:
    signal_id: str
    direction: str
    signal_time: datetime
    entry_time: datetime
    planned_entry: float
    fill: float
    sl: float
    tp1: float
    tp2: float
    lots: float
    oz: float
    risk_usd: float
    balance_before: float
    exit_time: datetime | None = None
    exit_reason: str = ""
    exit_price: float = 0.0          # volume-weighted exit price
    tp1_hit: bool = False
    swap_usd: float = 0.0
    pnl_usd: float = 0.0
    r_multiple: float = 0.0
    reason: str = ""


@dataclass
class Skip:
    time: datetime
    direction: str
    reason: str


@dataclass
class BacktestResult:
    trades: list[Trade]
    skips: list[Skip]
    equity: pd.Series                 # balance after each closed trade, indexed by exit time
    initial_balance: float
    start: pd.Timestamp
    end: pd.Timestamp
    params: dict = field(default_factory=dict)

    def trades_df(self) -> pd.DataFrame:
        return pd.DataFrame([asdict(t) for t in self.trades])

    def skips_df(self) -> pd.DataFrame:
        return pd.DataFrame([asdict(s) for s in self.skips])


def _swap_nights(index: pd.DatetimeIndex, triple_weekday: int, rollover_hm: str) -> np.ndarray:
    """Weighted number of rollovers in (index[i-1], index[i]] for each bar close time."""
    h, m = map(int, rollover_hm.split(":"))
    ny = index.tz_convert(NY).tz_localize(None) - pd.Timedelta(hours=h, minutes=m)
    day = ny.normalize()
    nights = np.zeros(len(index))
    for i in np.where(day[1:] != day[:-1])[0] + 1:
        # every rollover date crossed between bars i-1 and i
        for d in pd.date_range(day[i - 1] + pd.Timedelta(days=1), day[i], freq="D"):
            wd = d.weekday()               # weekday of the trading day that ended at this rollover
            if wd < 5:
                nights[i] += 3 if wd == triple_weekday else 1
    return nights


class Backtester:
    def __init__(self, cfg, features: pd.DataFrame, news: NewsFilter | None,
                 account: AccountType | None = None):
        self.cfg = cfg
        self.p = dict(cfg.strategy)
        self.f = features
        self.news = news
        self.sessions = SessionFilter(cfg.sessions.windows)
        self.acct = account or AccountType.from_config(cfg)
        c = cfg.costs
        self.half = c.spread / 2
        self.slip_in, self.slip_stop = c.slippage_entry, c.slippage_stop
        self.extra_cost = c.spread + c.slippage_entry + c.slippage_stop
        self.swap_rate = {1: c.swap_long_annual_pct / 100 / 365, -1: c.swap_short_annual_pct / 100 / 365}
        self.nights = _swap_nights(features.index, c.triple_swap_weekday, c.rollover_time_ny)
        ny = features.index.tz_convert(NY)
        self.weekend_exit = np.asarray((ny.weekday == 4) & (ny.hour * 60 + ny.minute >= 16 * 60 + 30))

    def run(self, start=None, end=None, initial_balance: float | None = None) -> BacktestResult:
        f = self.f
        idx = f.index
        start = pd.Timestamp(start or idx[0])
        end = pd.Timestamp(end or idx[-1])
        balance = float(initial_balance if initial_balance is not None else self.cfg.backtest.initial_balance)
        risk = RiskManager.from_config(self.cfg)
        o, h, l, c = (f[k].to_numpy() for k in ("open", "high", "low", "close"))

        cands = signal_candidates(f[(idx >= start) & (idx <= end)], self.p, self.sessions, self.news)
        cand_pos = {idx.get_loc(t): t for t in cands.index}

        trades: list[Trade] = []
        skips: list[Skip] = []
        eq_t, eq_v = [start], [balance]
        pos: Trade | None = None
        sgn = 0
        remaining_lots = 0.0
        cur_sl = cur_tp = 0.0
        realized = 0.0

        i0, i1 = idx.searchsorted(start), idx.searchsorted(end, side="right")
        i = i0
        while i < i1:
            # ---------- manage an open position on bar i ----------
            if pos is not None:
                ts = idx[i]
                px_o, px_h, px_l, px_c = (x - sgn * self.half for x in (o[i], h[i], l[i], c[i]))
                adverse = px_l if sgn == 1 else px_h
                favorable = px_h if sgn == 1 else px_l
                oz_rem = remaining_lots * self.acct.oz_per_lot
                if self.nights[i] and idx[i] > pd.Timestamp(pos.entry_time):
                    sw = oz_rem * c[i] * self.swap_rate[sgn] * self.nights[i]
                    pos.swap_usd -= sw
                exit_px = None
                reason = ""
                if sgn * (px_o - cur_sl) < 0 and ts - pd.Timedelta(minutes=15) > pd.Timestamp(pos.entry_time):
                    exit_px, reason = px_o - sgn * self.slip_stop, "SL gap"
                elif sgn * (adverse - cur_sl) <= 0:
                    exit_px, reason = cur_sl - sgn * self.slip_stop, "SL"
                elif sgn * (favorable - cur_tp) >= 0:
                    if not pos.tp1_hit:
                        part = split_lots(remaining_lots, self.acct, self.p["tp1_close_fraction"])
                        pos.tp1_hit = True
                        if part > 0:
                            realized += sgn * (pos.tp1 - pos.fill) * part * self.acct.oz_per_lot
                            remaining_lots = round(remaining_lots - part, 8)
                            if self.p["move_sl_to_be_after_tp1"]:
                                cur_sl = pos.fill
                            cur_tp = pos.tp2
                            if sgn * (favorable - cur_tp) >= 0:
                                exit_px, reason = cur_tp, "TP2"
                        else:
                            exit_px, reason = cur_tp, "TP1"
                    else:
                        exit_px, reason = cur_tp, "TP2"
                if exit_px is None:
                    held_h = (ts - pd.Timestamp(pos.entry_time)).total_seconds() / 3600
                    if self.p["flat_before_weekend"] and self.weekend_exit[i]:
                        exit_px, reason = px_c, "weekend close"
                    elif held_h >= self.p["max_hold_hours"]:
                        exit_px, reason = px_c, "time stop"
                if exit_px is not None:
                    if reason.startswith("SL") and pos.tp1_hit and self.p["move_sl_to_be_after_tp1"]:
                        reason = "breakeven after TP1"
                    realized += sgn * (exit_px - pos.fill) * remaining_lots * self.acct.oz_per_lot
                    pnl = realized + pos.swap_usd
                    pos.exit_time = ts.to_pydatetime()
                    pos.exit_reason = reason
                    pos.exit_price = round(pos.fill + sgn * realized / max(pos.oz, 1e-12), 3)
                    pos.pnl_usd = pnl
                    pos.r_multiple = pnl / pos.risk_usd if pos.risk_usd else 0.0
                    risk.on_close(ts.to_pydatetime(), pnl, balance)
                    balance += pnl
                    trades.append(pos)
                    eq_t.append(ts)
                    eq_v.append(balance)
                    pos = None
                i += 1
                continue

            # ---------- look for a new signal at the close of bar i ----------
            if i in cand_pos:
                ts = idx[i]
                sig, why = build_signal(f.iloc[i], ts, self.p)
                if sig is None:
                    skips.append(Skip(ts.to_pydatetime(), "", why))
                    i += 1
                    continue
                ok, why = risk.can_open(ts.to_pydatetime(), balance)
                if not ok:
                    skips.append(Skip(ts.to_pydatetime(), sig.direction, why))
                    i += 1
                    continue
                size = size_position(balance, sig.risk_per_oz, self.acct, self.cfg.account.risk_pct,
                                     self.cfg.account.max_risk_pct, self.extra_cost)
                if size.skip:
                    skips.append(Skip(ts.to_pydatetime(), sig.direction, size.note))
                    i += 1
                    continue
                j = i + 1
                if j >= len(idx) or idx[j] - ts != pd.Timedelta(minutes=15):
                    skips.append(Skip(ts.to_pydatetime(), sig.direction, "no contiguous next bar"))
                    i += 1
                    continue
                s = 1 if sig.direction == "BUY" else -1
                fill = o[j] + s * (self.half + self.slip_in)
                exit_side_open = o[j] - s * self.half
                if s * (exit_side_open - sig.sl) <= 0 or s * (fill - sig.tp1) >= 0:
                    skips.append(Skip(ts.to_pydatetime(), sig.direction, "price passed SL/TP1 before entry"))
                    i += 1
                    continue
                risk.on_open(ts.to_pydatetime(), balance)
                pos = Trade(sig.id, sig.direction, sig.time, (idx[j] - pd.Timedelta(minutes=15)).to_pydatetime(),
                            sig.entry, fill, sig.sl, sig.tp1, sig.tp2, size.lots, size.oz, size.risk_usd,
                            balance, reason=sig.reason)
                sgn, remaining_lots, cur_sl, cur_tp, realized = s, size.lots, sig.sl, sig.tp1, 0.0
                i = j          # manage from the entry bar itself
                continue
            i += 1

        if pos is not None:   # close anything still open at the end of the data
            px = c[i1 - 1] - sgn * self.half
            realized += sgn * (px - pos.fill) * remaining_lots * self.acct.oz_per_lot
            pos.exit_time, pos.exit_reason = idx[i1 - 1].to_pydatetime(), "end of data"
            pos.pnl_usd = realized + pos.swap_usd
            pos.r_multiple = pos.pnl_usd / pos.risk_usd
            balance += pos.pnl_usd
            trades.append(pos)
            eq_t.append(idx[i1 - 1])
            eq_v.append(balance)
        equity = pd.Series(eq_v, index=pd.DatetimeIndex(eq_t))
        return BacktestResult(trades, skips, equity, eq_v[0], start, end, dict(self.p))
