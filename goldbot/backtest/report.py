"""Equity-curve chart and markdown report."""
from __future__ import annotations

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402

SURFACE, INK, INK2, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#e4e3df"
SERIES = ["#2a78d6", "#eb6834"]


def equity_chart(curves: dict[str, pd.Series], path, title: str) -> None:
    """curves: label -> balance series (same $ units, so one shared axis)."""
    fig, (ax, ax2) = plt.subplots(2, 1, figsize=(11, 6.5), sharex=True,
                                  gridspec_kw={"height_ratios": [3, 1.2]}, facecolor=SURFACE)
    for a in (ax, ax2):
        a.set_facecolor(SURFACE)
        a.grid(axis="y", color=GRID, linewidth=0.8)
        for side in ("top", "right"):
            a.spines[side].set_visible(False)
        for side in ("left", "bottom"):
            a.spines[side].set_color(GRID)
        a.tick_params(colors=INK2, labelsize=9)
    for k, (label, s) in enumerate(curves.items()):
        if s.empty:
            continue
        col = SERIES[k % len(SERIES)]
        ax.step(s.index, s.values, where="post", color=col, linewidth=2, label=label)
        ax.annotate(f"${s.iloc[-1]:.2f}", (s.index[-1], s.iloc[-1]), xytext=(6, 0), textcoords="offset points",
                    color=INK, fontsize=9, va="center")
        dd = 100 * (s / s.cummax() - 1)
        ax2.step(dd.index, dd.values, where="post", color=col, linewidth=1.5)
    start_bal = next(iter(curves.values())).iloc[0]
    ax.axhline(start_bal, color=INK2, linewidth=0.8, linestyle=(0, (4, 3)))
    ax.set_ylabel("Balance (USD)", color=INK2, fontsize=10)
    ax2.set_ylabel("Drawdown %", color=INK2, fontsize=10)
    ax.set_title(title, color=INK, fontsize=12, loc="left")
    ax.legend(frameon=False, fontsize=9, labelcolor=INK)
    ax2.xaxis.set_major_locator(mdates.YearLocator())
    ax2.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
    fig.tight_layout()
    fig.savefig(path, dpi=150, facecolor=SURFACE)
    plt.close(fig)


def md_table(rows: list[tuple[str, ...]], header: tuple[str, ...]) -> str:
    out = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    out += ["| " + " | ".join(r) + " |" for r in rows]
    return "\n".join(out)
