"""SQLite candle cache. One table, keyed by (symbol, interval, open time)."""
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

import pandas as pd

from .provider import COLUMNS, empty_frame

SCHEMA = """
CREATE TABLE IF NOT EXISTS candles (
    symbol   TEXT NOT NULL,
    interval TEXT NOT NULL,
    ts       INTEGER NOT NULL,          -- candle open time, unix seconds UTC
    open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL, close REAL NOT NULL,
    PRIMARY KEY (symbol, interval, ts)
) WITHOUT ROWID;
"""


class CandleCache:
    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._con() as con:
            con.executescript(SCHEMA)

    def _con(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

    def upsert(self, symbol: str, interval: str, df: pd.DataFrame) -> int:
        if df.empty:
            return 0
        ts = (df.index.as_unit("s").asi8).tolist()
        rows = [(symbol, interval, t, *vals) for t, vals in zip(ts, df[COLUMNS].itertuples(index=False, name=None))]
        with self._con() as con:
            con.executemany("INSERT OR REPLACE INTO candles VALUES (?,?,?,?,?,?,?)", rows)
        return len(rows)

    def load(self, symbol: str, interval: str, start: datetime | None = None,
             end: datetime | None = None) -> pd.DataFrame:
        q = "SELECT ts, open, high, low, close FROM candles WHERE symbol=? AND interval=?"
        args: list = [symbol, interval]
        if start is not None:
            q += " AND ts >= ?"
            args.append(int(pd.Timestamp(start).timestamp()))
        if end is not None:
            q += " AND ts <= ?"
            args.append(int(pd.Timestamp(end).timestamp()))
        q += " ORDER BY ts"
        with self._con() as con:
            df = pd.read_sql_query(q, con, params=args)
        if df.empty:
            return empty_frame()
        df.index = pd.to_datetime(df.pop("ts"), unit="s", utc=True).rename("time")
        return df.astype("float64")

    def bounds(self, symbol: str, interval: str) -> tuple[pd.Timestamp | None, pd.Timestamp | None]:
        with self._con() as con:
            lo, hi = con.execute("SELECT MIN(ts), MAX(ts) FROM candles WHERE symbol=? AND interval=?",
                                 (symbol, interval)).fetchone()
        if lo is None:
            return None, None
        return pd.Timestamp(lo, unit="s", tz="UTC"), pd.Timestamp(hi, unit="s", tz="UTC")
