"""Command line entry point: python -m goldbot <command>."""
from __future__ import annotations

import argparse
import logging
from datetime import datetime, timezone

import pandas as pd
from dotenv import load_dotenv

from .config import load_config, resolve_path


def _data_service(cfg):
    from .data.cache import CandleCache
    from .data.providers import make_provider
    from .data.service import DataService
    return DataService(make_provider(cfg), CandleCache(resolve_path(cfg.general.db_path)), cfg.general.symbol)


def cmd_fetch(cfg, args):
    svc = _data_service(cfg)
    start = pd.Timestamp(args.start or cfg.data.history_start, tz="UTC")
    end = datetime.now(timezone.utc)
    for iv in args.intervals or cfg.data.intervals:
        n = svc.sync(iv, start=start, end=end)
        lo, hi = svc.cache.bounds(cfg.general.symbol, iv)
        print(f"{iv}: +{n} bars, cache spans {lo} -> {hi}")
        print("   ", svc.gap_report(iv, lo, hi))
    print(f"API requests used today: {svc.provider.limiter.used_today()}")


def cmd_backtest(cfg, args):
    from .backtest.runner import run_full_backtest
    run_full_backtest(cfg, _data_service(cfg))


def main(argv=None):
    load_dotenv(resolve_path(".env"))
    p = argparse.ArgumentParser(prog="goldbot", description="XAU/USD signal bot (signal-only, never trades)")
    p.add_argument("--config", default=None)
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch", help="download/refresh candles into the local cache")
    f.add_argument("--start", default=None)
    f.add_argument("--intervals", nargs="*")
    sub.add_parser("backtest", help="full-period backtest + walk-forward validation + report")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_config(args.config)
    {"fetch": cmd_fetch, "backtest": cmd_backtest}[args.cmd](cfg, args)


if __name__ == "__main__":
    main()
