"""Load config.toml into a read-only nested namespace."""
from __future__ import annotations

import copy
import tomllib
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = ROOT / "config.toml"


class Section(dict):
    """dict with attribute access (cfg.strategy.sl_atr_mult)."""

    def __getattr__(self, key: str) -> Any:
        try:
            return self[key]
        except KeyError as exc:
            raise AttributeError(f"config has no key '{key}'") from exc

    def __setattr__(self, key: str, value: Any) -> None:
        self[key] = value


def _wrap(obj: Any) -> Any:
    if isinstance(obj, dict):
        return Section({k: _wrap(v) for k, v in obj.items()})
    if isinstance(obj, list):
        return [_wrap(v) for v in obj]
    return obj


def load_config(path: str | Path | None = None) -> Section:
    path = Path(path) if path else DEFAULT_CONFIG
    with open(path, "rb") as fh:
        cfg = _wrap(tomllib.load(fh))
    validate(cfg)
    return cfg


def with_overrides(cfg: Section, section: str, **values: Any) -> Section:
    """Deep copy of cfg with some keys in one section replaced (used by walk-forward)."""
    new = copy.deepcopy(cfg)
    for k, v in values.items():
        if k not in new[section]:
            raise KeyError(f"unknown {section} parameter: {k}")
        new[section][k] = v
    return new


def resolve_path(p: str) -> Path:
    path = Path(p)
    return path if path.is_absolute() else ROOT / path


def validate(cfg: Section) -> None:
    acct = cfg.account
    if acct.type not in cfg.account_types:
        raise ValueError(f"account.type '{acct.type}' not in [account_types]")
    if not 0 < acct.risk_pct <= min(acct.max_risk_pct, 2.0):
        raise ValueError("account.risk_pct must be > 0 and <= 2.0")
    if cfg.strategy.tp1_r < 2.0:
        raise ValueError("strategy.tp1_r must be >= 2.0 (minimum 1:2 risk/reward)")
    if cfg.strategy.momentum not in ("rsi", "macd"):
        raise ValueError("strategy.momentum must be 'rsi' or 'macd'")
    if cfg.general.mode not in ("paper", "live"):
        raise ValueError("general.mode must be 'paper' or 'live'")
