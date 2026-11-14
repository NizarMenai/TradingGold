"""Runs the full validation suite and writes reports/backtest_report.md + the equity chart."""
from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import pandas as pd

from ..config import resolve_path, with_overrides
from ..news import make_news_filter
from ..risk import AccountType
from ..strategy import prepare
from . import metrics as M
from .engine import Backtester
from .report import equity_chart, md_table
from .walkforward import run_walkforward


def _metrics(res) -> M.Metrics:
    return M.compute(res.trades_df(), res.equity, res.initial_balance, res.start, res.end)


def _summary_row(name: str, m: M.Metrics, ci=None) -> tuple[str, ...]:
    pf = "inf" if np.isinf(m.profit_factor) else f"{m.profit_factor:.2f}"
    ci_s = f"[{ci[0]:+.2f}, {ci[1]:+.2f}]" if ci else ""
    return (name, str(m.trades), f"{100 * m.win_rate:.1f}%", pf, f"{m.expectancy_r:+.3f}", ci_s,
            f"${m.expectancy_usd:+.3f}", f"{m.max_drawdown_pct:.1f}%", str(m.longest_losing_streak),
            f"${m.final_balance:.2f}")


HEADER = ("Run", "Trades", "Win rate", "PF", "Exp. R", "95% CI (R)", "Exp. $", "Max DD", "Lose streak", "Final $")


def run_full_backtest(cfg, svc) -> dict:
    sym = cfg.general.symbol
    bt = cfg.backtest
    df15, df1h, df4h = (svc.candles(iv) for iv in ("15min", "1h", "4h"))
    if df15.empty:
        raise SystemExit("No cached candles. Run: python -m goldbot fetch")
    start = max(pd.Timestamp(bt.start, tz="UTC"), df15.index[0] + pd.Timedelta(days=35))
    end = min(pd.Timestamp(bt.end, tz="UTC") + pd.Timedelta(days=1), df15.index[-1] + pd.Timedelta(minutes=15))
    lock = pd.Timestamp(bt.lockbox_start, tz="UTC")
    news = make_news_filter(cfg)
    p = dict(cfg.strategy)
    feat = prepare(df15, df1h, df4h, p)

    lines: list[str] = []
    w = lines.append
    w("# GoldBot backtest report\n")
    w(f"Generated {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC. Symbol {sym}, data from {svc.provider.name}.\n")
    w(f"Period: **{start.date()} to {end.date()}** ({(end - start).days / 365.25:.1f} years). "
      f"Account type `{cfg.account.type}`, starting balance ${bt.initial_balance:.0f}, "
      f"risk {cfg.account.risk_pct}% per trade.\n")
    c = cfg.costs
    w(f"Costs: spread ${c.spread:.2f}/oz, entry slippage ${c.slippage_entry:.2f}, stop slippage "
      f"${c.slippage_stop:.2f}, swap {c.swap_long_annual_pct}%/yr long, {c.swap_short_annual_pct}%/yr short.\n")

    w("## Data quality\n")
    for iv in ("15min", "1h"):
        w(f"- {svc.gap_report(iv, start, end)}")
    w(f"- 4h: {len(df4h)} bars cached\n")

    # 1) Defaults on the development period and the untouched lockbox
    dev = Backtester(cfg, feat, news).run(start, lock)
    box = Backtester(cfg, feat, news).run(lock, end)
    full = Backtester(cfg, feat, news).run(start, end)

    # 2) Walk-forward
    wf_folds, oos = run_walkforward(cfg, df15, df1h, df4h, news, start, end)
    wf_eq = M.r_equity(oos, bt.initial_balance, cfg.account.risk_pct) if not oos.empty else pd.Series(dtype=float)
    if not wf_eq.empty:
        wf_eq = pd.concat([pd.Series([bt.initial_balance], index=[pd.Timestamp(wf_folds[0].test_start)]), wf_eq])
    oos_m = M.compute(oos, wf_eq, bt.initial_balance, wf_folds[0].test_start, end) if not oos.empty else None

    rows = []
    for name, res in (("Defaults, development period", dev), ("Defaults, lockbox (last 12 months)", box),
                      ("Defaults, full period", full)):
        rows.append(_summary_row(name, _metrics(res), M.bootstrap_expectancy_ci(res.trades_df()["r_multiple"].to_numpy())
                                 if res.trades else None))
    if oos_m:
        rows.append(_summary_row("Walk-forward out-of-sample*", oos_m,
                                 M.bootstrap_expectancy_ci(oos["r_multiple"].to_numpy())))
    w("## Headline results\n")
    w(md_table(rows, HEADER))
    w("\n*Walk-forward balance uses the R-multiples compounded at the configured risk (no lot rounding).\n")

    w("## Full-period detail (default parameters)\n")
    w(md_table(_metrics(full).as_rows(), ("Metric", "Value")))
    tdf = full.trades_df()
    if not tdf.empty:
        w("\nExit reasons: " + ", ".join(f"{k} {v}" for k, v in tdf["exit_reason"].value_counts().items()))
        w("\nBy direction: " + ", ".join(
            f"{d} {len(g)} trades, {g['r_multiple'].mean():+.3f} R" for d, g in tdf.groupby("direction")))
        yearly = tdf.groupby(pd.to_datetime(tdf["exit_time"], utc=True).dt.year)["r_multiple"].agg(["count", "mean", "sum"])
        w("\n" + md_table([(str(y), str(int(r["count"])), f"{r['mean']:+.3f}", f"{r['sum']:+.1f}")
                           for y, r in yearly.iterrows()], ("Year", "Trades", "Exp. R", "Total R")))
    sk = full.skips_df()
    if not sk.empty:
        w("\nSkipped signals: " + ", ".join(f"{k} ({v})" for k, v in
                                            sk["reason"].str.replace(r"\d+(\.\d+)?", "#", regex=True).value_counts().head(6).items()))

    # 3) Cost sensitivity
    w("\n## Cost sensitivity (default parameters, full period)\n")
    crow = []
    for label, mult in (("No costs", 0.0), ("Base costs", 1.0), ("2x costs", 2.0)):
        cc = with_overrides(cfg, "costs", spread=c.spread * mult, slippage_entry=c.slippage_entry * mult,
                            slippage_stop=c.slippage_stop * mult, swap_long_annual_pct=c.swap_long_annual_pct * mult,
                            swap_short_annual_pct=c.swap_short_annual_pct * mult)
        crow.append(_summary_row(label, _metrics(Backtester(cc, feat, news).run(start, end))))
    w(md_table(crow, HEADER))

    # 4) Account types
    w("\n## Account type check (default parameters, full period)\n")
    arow = []
    for name in cfg.account_types:
        res = Backtester(cfg, feat, news, AccountType.from_config(cfg, name)).run(start, end)
        n_risk_skip = sum("risk too high" in s.reason for s in res.skips)
        m = _metrics(res)
        arow.append((name, str(m.trades), str(n_risk_skip), f"{m.expectancy_r:+.3f}", f"${m.final_balance:.2f}"))
    w(md_table(arow, ("Account", "Trades taken", "SKIP: risk too high", "Exp. R", "Final $")))

    # 5) Walk-forward folds
    w("\n## Walk-forward folds\n")
    frows = []
    for f in wf_folds:
        tm = _metrics(f.test_result)
        frows.append((f"{f.test_start.date()}..{f.test_end.date()}",
                      ", ".join(f"{k}={v}" for k, v in f.chosen.items()),
                      f"{f.train_expectancy_r:+.3f} ({f.train_trades})" if f.train_expectancy_r > -1e8 else "n/a",
                      str(tm.trades), f"{tm.expectancy_r:+.3f}"))
    w(md_table(frows, ("Test window", "Chosen on train", "Train exp. R (n)", "Test trades", "Test exp. R")))

    chart = resolve_path(bt.chart_path)
    chart.parent.mkdir(parents=True, exist_ok=True)
    equity_chart({"Default parameters (lot-rounded, cent account)": full.equity,
                  "Walk-forward out-of-sample (R compounded at 1%)": wf_eq},
                 chart, f"XAU/USD trend-pullback strategy: $50 account, costs included")
    w(f"\n![Equity curve]({chart.relative_to(resolve_path('reports'))})\n")

    report = resolve_path(bt.report_path)
    report.write_text("\n".join(lines) + "\n")
    full.trades_df().to_csv(resolve_path("reports/trades_full.csv"), index=False)
    if not oos.empty:
        oos.to_csv(resolve_path("reports/trades_walkforward_oos.csv"), index=False)
    print("\n".join(lines))
    return {"full": full, "dev": dev, "lockbox": box, "oos": oos, "folds": wf_folds}
