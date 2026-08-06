"""交易计划生成单测 + 选股器评分单测."""

from __future__ import annotations

from app.core.plan.generator import PlanGenerator
from app.core.screener.engine import score_indicators
from app.core.signals.engine import Signal

gen = PlanGenerator()


def _signal() -> Signal:
    return Signal(
        type="BUY_ADD", symbol="300750", name="宁德时代", direction="buy",
        strength=78.0, reason="回踩20日均线企稳,MACD未死叉", price=110.0,
        indicators_snapshot={"ma20": 105.0},
    )


def test_plan_generate_buy_add(tmp_engine):
    from app.core.position import position_manager

    position_manager.open_or_add("300750", "宁德时代", 100, 100.0, "首仓", None)
    plan = gen.generate("300750", "宁德时代", _signal(), portfolio={"total_pct": 65.0})
    assert plan["action"] == "buy_add"
    content = plan["content"]
    assert "交易计划" in content
    assert "宁德时代(300750)" in content
    assert "加仓" in content
    assert "止损价位" in content
    assert "风控检查" in content


def test_plan_generate_stop(tmp_engine):
    from app.core.position import position_manager

    position_manager.open_or_add("000001", "平安银行", 100, 100.0, "首仓", None)
    sig = Signal(type="SELL_STOP", symbol="000001", name="平安银行", direction="sell",
                 strength=90.0, reason="跌破止损线", price=90.0)
    plan = gen.generate("000001", "平安银行", sig, portfolio={"total_pct": 20.0})
    assert plan["action"] == "sell_stop"
    assert "止损" in plan["content"]


def test_plan_generate_buy_first(tmp_engine):
    sig = Signal(type="BUY_FIRST", symbol="600001", name="测试", direction="buy",
                 strength=72.0, reason="三共振入场", price=10.0)
    plan = gen.generate("600001", "测试", sig)
    assert plan["action"] == "buy_first"
    assert "首仓买入" in plan["content"]


# ---------------------------------------------------------------- 选股器评分
def test_score_indicators_uptrend(kline_df):
    from app.core.indicators import compute_all

    ind = compute_all(kline_df)
    score = score_indicators(ind)
    # 确定性上涨行情: 趋势分应显著 > 0
    assert score["trend_score"] > 10
    assert score["total"] > 0
    assert score["attention"] in ("强烈关注", "重点观察", "一般关注", "观察")


def test_score_indicators_downtrend_ranks_low():
    import numpy as np
    import pandas as pd
    from app.core.indicators import compute_all

    n = 120
    close = np.linspace(100, 50, n)
    df = pd.DataFrame({
        "date": pd.bdate_range("2025-01-02", periods=n).strftime("%Y-%m-%d"),
        "open": close + 0.2, "high": close + 0.5, "low": close - 0.5, "close": close,
        "volume": np.full(n, 1e6), "amount": np.full(n, 1e8),
    })
    ind = compute_all(df)
    score = score_indicators(ind)
    # 下跌趋势: 动量/量能低分, 总分明显低于上涨行情(ADX 只测强度不测方向, 趋势分可高)
    assert score["total"] < 50
