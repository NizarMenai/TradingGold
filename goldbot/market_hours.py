"""XAU/USD spot trading hours.

Spot gold trades Sunday 18:00 to Friday 17:00 New York time, with a daily break
from 17:00 to 18:00 New York time. Using New York time handles DST automatically.
Holiday closures (Christmas, New Year) show up as data gaps and are tolerated.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

NY = ZoneInfo("America/New_York")
CLOSE = time(17, 0)
OPEN = time(18, 0)


def is_market_open(ts: datetime) -> bool:
    """True if spot gold normally trades at instant ts (tz-aware)."""
    ny = ts.astimezone(NY)
    wd, t = ny.weekday(), ny.time()
    if wd == 5:                      # Saturday
        return False
    if wd == 4 and t >= CLOSE:       # Friday after close
        return False
    if wd == 6 and t < OPEN:         # Sunday before open
        return False
    if CLOSE <= t < OPEN:            # daily maintenance break
        return False
    return True


def open_mask(index: pd.DatetimeIndex) -> pd.Series:
    """Vectorised is_market_open for candle open times."""
    ny = index.tz_convert(NY)
    wd = ny.weekday
    mins = ny.hour * 60 + ny.minute
    close_m, open_m = CLOSE.hour * 60, OPEN.hour * 60
    closed = (wd == 5) | ((wd == 4) & (mins >= close_m)) | ((wd == 6) & (mins < open_m)) | \
             ((mins >= close_m) & (mins < open_m))
    return pd.Series(~closed, index=index)


def expected_bar_times(start: datetime, end: datetime, minutes: int) -> pd.DatetimeIndex:
    """Bar open times that should exist between start and end while the market is open."""
    idx = pd.date_range(pd.Timestamp(start).floor(f"{minutes}min"), end, freq=f"{minutes}min", tz="UTC")
    return idx[open_mask(idx).to_numpy()]


def last_expected_close(now: datetime, minutes: int = 15) -> datetime:
    """Close time of the most recent bar that should have completed by `now`."""
    t = pd.Timestamp(now).floor(f"{minutes}min")
    for _ in range(4 * 24 * 60 // minutes):
        if is_market_open((t - timedelta(minutes=minutes)).to_pydatetime()):
            return t.to_pydatetime()
        t -= timedelta(minutes=minutes)
    return t.to_pydatetime()
