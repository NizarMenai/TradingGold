"""Trading-session filter (London / New York), DST-aware via each window's own time zone."""
from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd


def _hm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


class SessionFilter:
    def __init__(self, windows: list[dict]):
        self.windows = [(w["name"], ZoneInfo(w["tz"]), _hm(w["start"]), _hm(w["end"])) for w in windows]

    def session_at(self, ts: datetime) -> str | None:
        """Name of the first open session at instant ts, or None. Weekends never count."""
        for name, tz, start, end in self.windows:
            local = ts.astimezone(tz)
            if local.weekday() < 5 and start <= local.time() < end:
                return name
        return None

    def mask(self, index: pd.DatetimeIndex) -> np.ndarray:
        ok = np.zeros(len(index), dtype=bool)
        for _, tz, start, end in self.windows:
            local = index.tz_convert(tz)
            mins = local.hour * 60 + local.minute
            s, e = start.hour * 60 + start.minute, end.hour * 60 + end.minute
            ok |= (local.weekday < 5) & (mins >= s) & (mins < e)
        return ok
