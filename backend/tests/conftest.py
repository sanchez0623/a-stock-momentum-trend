"""pytest 共享 fixture(用 fixture 行情数据, CI 不打真实上游)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest


@pytest.fixture
def kline_df() -> pd.DataFrame:
    """确定性模拟行情 200 根日线, 含一段上涨趋势."""
    rng = np.random.default_rng(42)
    n = 200
    dates = pd.bdate_range("2025-01-02", periods=n)
    close = 100 + np.cumsum(rng.normal(0, 0.8, n)) + np.linspace(0, 40, n)  # 缓涨
    open_ = close + rng.normal(0, 0.3, n)
    high = np.maximum(open_, close) + np.abs(rng.normal(0, 0.5, n))
    low = np.minimum(open_, close) - np.abs(rng.normal(0, 0.5, n))
    volume = rng.integers(1_000_000, 10_000_000, n).astype(float)
    amount = volume * close
    return pd.DataFrame({
        "date": dates.strftime("%Y-%m-%d"),
        "open": open_, "high": high, "low": low, "close": close,
        "volume": volume, "amount": amount,
    })
