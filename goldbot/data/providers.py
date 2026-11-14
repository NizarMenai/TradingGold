"""Provider registry: swap data sources by changing [data].provider in config.toml."""
from __future__ import annotations

from ..config import Section, resolve_path
from .provider import CandleProvider
from .ratelimit import RateLimiter


def make_provider(cfg: Section) -> CandleProvider:
    name = cfg.data.provider
    limiter = RateLimiter(cfg.data.max_requests_per_minute, cfg.data.max_requests_per_day,
                          db_path=str(resolve_path(cfg.general.db_path)))
    if name == "twelvedata":
        from .twelvedata import TwelveDataProvider
        return TwelveDataProvider(limiter=limiter)
    raise ValueError(f"unknown data provider '{name}'")
