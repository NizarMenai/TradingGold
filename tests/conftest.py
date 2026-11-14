import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from goldbot.config import load_config  # noqa: E402


@pytest.fixture
def cfg():
    return load_config()


def make_ohlc(closes, spread=1.0, start="2024-03-04 08:00", freq="15min"):
    closes = np.asarray(closes, dtype=float)
    opens = np.r_[closes[0], closes[:-1]]
    idx = pd.date_range(start, periods=len(closes), freq=freq, tz="UTC", name="time")
    return pd.DataFrame({"open": opens, "high": np.maximum(opens, closes) + spread / 2,
                         "low": np.minimum(opens, closes) - spread / 2, "close": closes}, index=idx)
