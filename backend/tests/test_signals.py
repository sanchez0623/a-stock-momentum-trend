"""信号引擎单测(纯函数, fixture 行情)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from app.core.signals import SignalEngine
from app.core.signals.engine import PositionInfo

engine = SignalEngine()


def _uptrend_df() -> pd.DataFrame:
    """构造"横盘后放量启动"形态: 前 90 根横盘 50, 后 30 根温和上涨至 62, 最后 5 天放量 2~4 倍."""
    n = 120
    rng = np.random.default_rng(3)
    dates = pd.bdate_range("2025-01-02", periods=n)
    base = np.full(n, 50.0)
    base[90:] += np.linspace(0, 12, 30)  # 后 30 根 50 -> 62
    close = base + rng.normal(0, 0.3, n)
    open_ = close - 0.2
    high = close + 0.4
    low = close - 0.4
    volume = np.full(n, 1_000_000.0)
    volume[115:] = [2e6, 2.5e6, 3e6, 3.5e6, 4e6]
    return pd.DataFrame({
        "date": dates.strftime("%Y-%m-%d"),
        "open": open_, "high": high, "low": low, "close": close,
        "volume": volume, "amount": volume * close,
    })


def _downtrend_df() -> pd.DataFrame:
    """构造下跌趋势(用于止损场景)."""
    n = 120
    dates = pd.bdate_range("2025-01-02", periods=n)
    close = np.linspace(100, 50, n) + np.random.default_rng(2).normal(0, 0.5, n)
    open_ = close + 0.3
    high = close + 0.5
    low = close - 0.5
    return pd.DataFrame({
        "date": dates.strftime("%Y-%m-%d"),
        "open": open_, "high": high, "low": low, "close": close,
        "volume": np.full(n, 1_000_000.0), "amount": np.full(n, 100_000_000.0),
    })


def test_buy_first_on_uptrend():
    df = _uptrend_df()
    sig = engine.evaluate("600001", "测试股", kline_df=df, quote_price=float(df.iloc[-1]["close"]))
    assert sig is not None
    assert sig.type == "BUY_FIRST"
    assert sig.direction == "buy"
    assert 0 <= sig.strength <= 100
    assert sig.reason


def test_buy_add_with_profit_position():
    df = _uptrend_df()
    price = float(df.iloc[-1]["close"])
    # 浮盈 2%(不触发 3% 止盈档), 满足回踩加仓条件
    pos = PositionInfo(symbol="600001", cost=price * 0.98, qty=1000)
    sig = engine.evaluate("600001", "测试股", kline_df=df, position=pos, quote_price=price)
    assert sig is not None and sig.type == "BUY_ADD"


def test_stop_on_breakdown():
    df = _downtrend_df()
    price = float(df.iloc[-1]["close"])
    pos = PositionInfo(symbol="600002", cost=price * 1.2, qty=1000)  # 已亏 16% > 5% 止损线
    sig = engine.evaluate("600002", "测试股", kline_df=df, position=pos, quote_price=price)
    assert sig is not None and sig.type == "SELL_STOP"
    assert sig.strength >= 90


def test_no_signal_without_position_and_weak_trend():
    df = _downtrend_df()
    sig = engine.evaluate("600003", "测试股", kline_df=df, quote_price=float(df.iloc[-1]["close"]))
    # 下跌趋势不产生 BUY_FIRST
    assert sig is None or sig.type != "BUY_FIRST"


def test_indicators_snapshot_present():
    df = _uptrend_df()
    sig = engine.evaluate("600001", "测试股", kline_df=df, quote_price=float(df.iloc[-1]["close"]))
    if sig:
        assert "ma20" in sig.indicators_snapshot
        assert "rsi14" in sig.indicators_snapshot


def test_t_trade_requires_position():
    df = _uptrend_df()
    # 无持仓时不做 T
    sig = engine.evaluate("600004", "测试股", kline_df=df, quote_price=float(df.iloc[-1]["close"]))
    assert sig is None or sig.type not in ("T_BUY", "T_SELL")


def test_atr_dynamic_take_profit_targets():
    """ATR 动态止盈档: 波动大档位远, 带下限保护."""
    import pandas as pd
    from app.core.signals.engine import SignalEngine

    eng = SignalEngine()
    # 高波动(ATR 5%)
    last = pd.Series({"atr14": 5.0, "close": 100.0})
    targets = eng.take_profit_targets(100.0, last)
    assert targets[0] > 100.0
    # 1.5×5% = 7.5% 首档
    assert targets[0] == pytest.approx(107.5, abs=0.01)
    # 低波动(ATR 1%) -> 下限保护 3%
    last2 = pd.Series({"atr14": 1.0, "close": 100.0})
    targets2 = eng.take_profit_targets(100.0, last2)
    assert targets2[0] == pytest.approx(103.0, abs=0.01)


def test_atr_hit_take_profit():
    import pandas as pd
    from app.core.signals.engine import SignalEngine

    eng = SignalEngine()
    last = pd.Series({"atr14": 3.0, "close": 100.0})  # ATR 3% -> 首档 1.5*3%=4.5%
    # 现价 103(浮盈3%) 未达首档 104.5
    assert eng._hit_take_profit(103.0, 100.0, last) is None
    # 现价 105 达首档
    assert eng._hit_take_profit(105.0, 100.0, last) == pytest.approx(1.045, abs=0.001)
