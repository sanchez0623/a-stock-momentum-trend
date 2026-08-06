"""技术指标单测(纯函数, 确定性验证)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from app.core.indicators import (
    adx,
    atr,
    bollinger,
    compute_all,
    donchian,
    ema,
    ma,
    macd,
    roc,
    rsi,
    volume_ratio,
)


def test_ma_basic():
    df = pd.DataFrame({"close": [1.0, 2.0, 3.0, 4.0, 5.0]})
    out = ma(df, 3)
    assert out["ma3"].iloc[-1] == pytest.approx(4.0)
    assert np.isnan(out["ma3"].iloc[0])


def test_ema_span():
    df = pd.DataFrame({"close": [1.0, 2.0, 3.0]})
    out = ema(df, 2)
    # ewm(span=2, adjust=False): e1=1, e2=(2*2/3)+(1*1/3)=1.6667, e3=(3*2/3)+(1.6667*1/3)=2.5556
    assert out["ema2"].iloc[-1] == pytest.approx(2.5556, abs=1e-3)


def test_macd_columns_and_cross():
    df = pd.DataFrame({"close": np.linspace(10, 20, 60)})
    out = macd(df)
    for col in ("dif", "dea", "macd_hist"):
        assert col in out.columns
    assert out["dif"].iloc[-1] > out["dea"].iloc[-1]  # 上涨趋势 dif > dea


def test_rsi_bounds(kline_df):
    out = rsi(kline_df, 14)
    vals = out["rsi14"].dropna()
    assert vals.between(0, 100).all()


def test_rsi_constant_rise_is_high():
    df = pd.DataFrame({"close": np.linspace(10, 20, 30)})
    out = rsi(df, 14)
    assert out["rsi14"].dropna().iloc[-1] > 90


def test_adx_columns_and_trend(kline_df):
    out = adx(kline_df, 14)
    for col in ("adx14", "pdi14", "mdi14"):
        assert col in out.columns
    # 确定性上涨趋势: ADX 尾部应大于 25(有趋势)
    assert out["adx14"].dropna().iloc[-1] > 25


def test_roc_positive_for_uptrend(kline_df):
    out = roc(kline_df, 12)
    assert out["roc12"].dropna().iloc[-1] > 0


def test_bollinger_order(kline_df):
    out = bollinger(kline_df, 20, 2.0)
    tail = out.dropna()
    assert (tail["boll_upper20"] >= tail["boll_mid20"]).all()
    assert (tail["boll_mid20"] >= tail["boll_lower20"]).all()


def test_donchian_bounds(kline_df):
    out = donchian(kline_df, 20)
    tail = out.dropna()
    assert (tail["dc_upper20"] >= tail["high"] - 1e-9).all()
    assert (tail["dc_lower20"] <= tail["low"] + 1e-9).all()


def test_volume_ratio_spike():
    df = pd.DataFrame({"volume": [100.0] * 29 + [300.0]})
    out = volume_ratio(df, 20)
    assert out["volume_ratio20"].dropna().iloc[-1] == pytest.approx(3.0, rel=0.05)


def test_atr_positive(kline_df):
    out = atr(kline_df, 14)
    assert (out["atr14"].dropna() > 0).all()


def test_compute_all_columns(kline_df):
    out = compute_all(kline_df)
    expected = [
        "ma10", "ma20", "ma60", "ema10", "ema20", "dif", "dea", "macd_hist",
        "rsi14", "adx14", "pdi14", "mdi14", "roc12", "boll_upper20", "boll_mid20",
        "boll_lower20", "dc_upper20", "dc_lower20", "volume_ratio20", "atr14",
    ]
    for col in expected:
        assert col in out.columns, f"缺少指标列 {col}"
    assert len(out) == len(kline_df)


def test_compute_all_preserves_input(kline_df):
    original = kline_df.copy()
    compute_all(kline_df)
    pd.testing.assert_frame_equal(kline_df, original)  # 纯函数, 不改输入
