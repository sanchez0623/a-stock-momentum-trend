"""动态阈值工具(volatility.py)单测."""
from __future__ import annotations

from app.core.volatility import atr_pct_from_ind, dynamic_threshold


def test_dynamic_threshold_basic():
    # 0.5 × 2.5% = 1.25%
    assert abs(dynamic_threshold(0.5, 0.025) - 0.0125) < 1e-9


def test_dynamic_threshold_floor():
    # 2 × 0.8% = 1.6% -> 下限 3% 兜底
    assert abs(dynamic_threshold(2.0, 0.008, floor=0.03) - 0.03) < 1e-9


def test_dynamic_threshold_ceil():
    # 2 × 10% = 20% -> 上限 15% 钳位
    assert abs(dynamic_threshold(2.0, 0.10, ceil=0.15) - 0.15) < 1e-9


def test_dynamic_threshold_regime_mult():
    # 市况系数: 震荡 1.2 -> 1.0 × 0.025 × 1.2 = 3%
    assert abs(dynamic_threshold(1.0, 0.025, regime="range") - 0.03) < 1e-9
    # 趋势强 0.6 -> 1.0 × 0.025 × 0.6 = 1.5%
    assert abs(dynamic_threshold(1.0, 0.025, regime="trend_strong") - 0.015) < 1e-9
    # 未知市况 -> 系数 1.0
    assert abs(dynamic_threshold(1.0, 0.025, regime="whatever") - 0.025) < 1e-9


def test_dynamic_threshold_zero_atr():
    # ATR 缺失返回 0(调用方走自身兜底)
    assert dynamic_threshold(0.5, 0.0) == 0.0


def test_atr_pct_from_ind_series():
    import pandas as pd

    last = pd.Series({"atr14": 0.5, "close": 20.0})
    assert abs(atr_pct_from_ind(last) - 0.025) < 1e-9


def test_atr_pct_from_ind_missing():
    import pandas as pd

    # 缺 atr14 / close<=0 / None 均返回 0; dict 输入同样支持
    assert atr_pct_from_ind(pd.Series({"close": 20.0})) == 0.0
    assert atr_pct_from_ind(pd.Series({"atr14": 0.5, "close": 0.0})) == 0.0
    assert atr_pct_from_ind(None) == 0.0
    assert abs(atr_pct_from_ind({"atr14": 0.5, "close": 20.0}) - 0.025) < 1e-9


def test_high_vol_vs_low_vol_stock_semantics():
    """语义验证: 同一倍数下, 高波动股阈值应高于低波动股."""
    high_vol = dynamic_threshold(0.5, 0.04)   # ATR 4%: 阈值 2%
    low_vol = dynamic_threshold(0.5, 0.01)    # ATR 1%: 阈值 0.5%
    assert high_vol > low_vol * 2
