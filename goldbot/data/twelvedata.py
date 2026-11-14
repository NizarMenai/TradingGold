"""Twelve Data provider (https://twelvedata.com/docs/market-data/time-series).

Free "Basic" plan, checked 2026-11-14: 8 API credits/minute, 800/day, 1 credit per
time_series call, up to 5000 bars per call. XAU/USD is available on Basic.
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime

import pandas as pd
import requests

from .provider import CandleProvider, ProviderError, RateLimitError, empty_frame, normalize
from .ratelimit import RateLimiter

log = logging.getLogger(__name__)
BASE_URL = "https://api.twelvedata.com"


class TwelveDataProvider(CandleProvider):
    name = "twelvedata"
    max_bars_per_request = 5000

    def __init__(self, api_key: str | None = None, limiter: RateLimiter | None = None,
                 session: requests.Session | None = None, timeout: float = 30.0, retries: int = 3):
        self.api_key = api_key or os.getenv("TWELVEDATA_API_KEY", "")
        self.limiter = limiter or RateLimiter(per_minute=7, per_day=780)
        self.session = session or requests.Session()
        self.timeout = timeout
        self.retries = retries

    def _get(self, path: str, params: dict) -> dict:
        headers = {"Authorization": f"apikey {self.api_key}"} if self.api_key else {}
        last_exc: Exception | None = None
        for attempt in range(self.retries):
            self.limiter.acquire()
            try:
                r = self.session.get(f"{BASE_URL}/{path}", params=params, headers=headers, timeout=self.timeout)
                data = r.json()
            except (requests.RequestException, ValueError) as exc:
                last_exc = exc
                time.sleep(2 ** attempt)
                continue
            if isinstance(data, dict) and data.get("status") == "error":
                code = data.get("code")
                msg = data.get("message", "")
                if code == 429:
                    log.warning("Twelve Data rate limit hit: %s", msg)
                    time.sleep(61)
                    last_exc = RateLimitError(msg)
                    continue
                if code == 400 and "No data is available" in msg:
                    return {"values": []}
                raise ProviderError(f"Twelve Data error {code}: {msg}")
            return data
        raise ProviderError(f"Twelve Data request failed after {self.retries} attempts: {last_exc}")

    def fetch(self, symbol: str, interval: str, start: datetime | None = None,
              end: datetime | None = None, outputsize: int | None = None) -> pd.DataFrame:
        params = {"symbol": symbol, "interval": interval, "timezone": "UTC", "order": "asc",
                  "outputsize": outputsize or self.max_bars_per_request}
        fmt = "%Y-%m-%d %H:%M:%S"
        if start is not None:
            params["start_date"] = start.strftime(fmt)
        if end is not None:
            params["end_date"] = end.strftime(fmt)
        data = self._get("time_series", params)
        values = data.get("values") or []
        if not values:
            return empty_frame()
        df = pd.DataFrame(values)
        df.index = pd.to_datetime(df.pop("datetime"), utc=True)
        return normalize(df)
