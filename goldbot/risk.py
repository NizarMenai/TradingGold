"""Position sizing and daily risk rules for a small account.

All money values are real USD (a cent account's 5000 USC balance is $50 here).
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class AccountType:
    name: str
    oz_per_lot: float
    min_lot: float
    lot_step: float

    @classmethod
    def from_config(cls, cfg, name: str | None = None) -> "AccountType":
        name = name or cfg.account.type
        t = cfg.account_types[name]
        return cls(name, float(t["oz_per_lot"]), float(t["min_lot"]), float(t["lot_step"]))


@dataclass(frozen=True)
class SizeResult:
    lots: float
    oz: float
    risk_usd: float              # loss in USD if the stop is hit, incl. spread + slippage
    risk_pct: float
    skip: bool
    note: str = ""


def cost_per_oz(spread: float, slippage_entry: float, slippage_stop: float) -> float:
    """Extra loss per ounce on a stopped trade beyond the entry-to-stop distance."""
    return spread + slippage_entry + slippage_stop


def size_position(balance: float, stop_distance: float, acct: AccountType, risk_pct: float,
                  max_risk_pct: float = 2.0, extra_cost_per_oz: float = 0.0) -> SizeResult:
    """Largest lot size whose stop-out loss stays within risk_pct of balance.

    If even the broker's minimum lot would lose more than risk_pct, the minimum lot is
    used only while it stays within max_risk_pct; otherwise the signal is a SKIP.
    """
    if balance <= 0 or stop_distance <= 0:
        return SizeResult(0.0, 0.0, 0.0, 0.0, True, "invalid balance or stop distance")
    risk_pct = min(risk_pct, max_risk_pct, 2.0)
    loss_per_oz = stop_distance + extra_cost_per_oz
    loss_per_lot = loss_per_oz * acct.oz_per_lot
    budget = balance * risk_pct / 100.0
    steps = math.floor(budget / loss_per_lot / acct.lot_step + 1e-9)
    lots = round(steps * acct.lot_step, 8)
    note = ""
    if lots < acct.min_lot:
        min_risk = acct.min_lot * loss_per_lot
        if min_risk <= balance * max_risk_pct / 100.0 + 1e-9:
            lots = acct.min_lot
            note = f"minimum lot risks {100 * min_risk / balance:.2f}% (above {risk_pct}% target, within {max_risk_pct}% cap)"
        else:
            return SizeResult(0.0, 0.0, min_risk, 100 * min_risk / balance, True,
                              f"SKIP - risk too high for balance: min lot {acct.min_lot} on a {acct.name} account "
                              f"would risk ${min_risk:.2f} ({100 * min_risk / balance:.1f}%)")
    risk_usd = lots * loss_per_lot
    return SizeResult(lots, lots * acct.oz_per_lot, risk_usd, 100 * risk_usd / balance, False, note)


def split_lots(lots: float, acct: AccountType, fraction: float) -> float:
    """Lots to close at TP1 (0 if the position is too small to split)."""
    part = math.floor(lots * fraction / acct.lot_step + 1e-9) * acct.lot_step
    if part < acct.min_lot or lots - part < acct.min_lot - 1e-9:
        return 0.0
    return round(part, 8)


@dataclass
class DailyRiskState:
    day: date | None = None
    start_balance: float = 0.0
    signals: int = 0
    losses: int = 0
    realized: float = 0.0


@dataclass
class RiskManager:
    """Enforces: one open signal, max N signals/day, stop after K losses or X% daily loss."""
    max_open: int = 1
    max_signals_per_day: int = 3
    max_losses_per_day: int = 2
    max_daily_loss_pct: float = 4.0
    tz: str = "UTC"
    open_count: int = 0
    state: DailyRiskState = field(default_factory=DailyRiskState)

    @classmethod
    def from_config(cls, cfg) -> "RiskManager":
        r = cfg.risk
        return cls(r.max_open_signals, r.max_signals_per_day, r.max_losses_per_day, r.max_daily_loss_pct,
                   r.day_boundary_tz)

    def _roll(self, ts: datetime, balance: float) -> None:
        d = ts.astimezone(ZoneInfo(self.tz)).date()
        if self.state.day != d:
            self.state = DailyRiskState(d, balance)

    def can_open(self, ts: datetime, balance: float) -> tuple[bool, str]:
        self._roll(ts, balance)
        s = self.state
        if self.open_count >= self.max_open:
            return False, "a signal is already open"
        if s.signals >= self.max_signals_per_day:
            return False, f"daily limit of {self.max_signals_per_day} signals reached"
        if s.losses >= self.max_losses_per_day:
            return False, f"stopped for the day after {s.losses} losses"
        if s.start_balance > 0 and -s.realized >= s.start_balance * self.max_daily_loss_pct / 100 - 1e-9:
            return False, f"daily loss limit of {self.max_daily_loss_pct}% reached"
        return True, ""

    def on_open(self, ts: datetime, balance: float) -> None:
        self._roll(ts, balance)
        self.state.signals += 1
        self.open_count += 1

    def on_close(self, ts: datetime, pnl: float, balance_before: float) -> None:
        self.open_count = max(0, self.open_count - 1)
        self._roll(ts, balance_before)
        self.state.realized += pnl
        if pnl < 0:
            self.state.losses += 1


class BalanceFile:
    """Human-editable JSON balance file: {"balance": 50.0, "updated": "..."}."""

    def __init__(self, path: str | Path, default: float):
        self.path = Path(path)
        self.default = default

    def read(self) -> float:
        try:
            return float(json.loads(self.path.read_text())["balance"])
        except (FileNotFoundError, KeyError, ValueError, json.JSONDecodeError):
            self.write(self.default)
            return self.default

    def write(self, balance: float) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"balance": round(balance, 2),
                                         "updated": datetime.now().astimezone().isoformat(timespec="seconds")},
                                        indent=2) + "\n")
