"""交易计划生成单测 + 选股器评分单测."""

from __future__ import annotations

from app import db
from app.core.plan.generator import PlanGenerator
from app.core.screener.engine import score_indicators
from app.core.signals.engine import Signal
from app.models.models import Position
from sqlmodel import select

gen = PlanGenerator()


def _backdate(symbol: str, opened_at: str = "2020-01-01 09:30:00") -> None:
    """把持仓时间改到过去, 解除 T+1 锁定."""
    with db.session_scope() as s:
        p = s.exec(select(Position).where(Position.symbol == symbol)).first()
        if p:
            p.opened_at = opened_at
            s.add(p)
            s.commit()


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


def test_plan_buy_add_shows_second_leg_for_held_position(tmp_engine):
    """复现用户 bug: 已持仓(仅首仓)时 BUY_ADD 计划应建议第2档30%, 而非第1档50%."""
    from app.core.position import position_manager

    position_manager.open_or_add("300139", "晓程科技", 3100, 45.85, "首仓", None)
    sig = Signal(type="BUY_ADD", symbol="300139", name="晓程科技", direction="buy",
                 strength=78.0, reason="回踩20日线企稳", price=48.0)
    plan = gen.generate("300139", "晓程科技", sig, portfolio={"total_pct": 45.0})
    assert plan["action"] == "buy_add"
    assert "第 2 档" in plan["content"]
    assert "30%" in plan["content"]
    assert "第 1 档" not in plan["content"]


def test_plan_buy_add_shows_third_leg_after_one_add(tmp_engine):
    """已加一档后, BUY_ADD 计划应建议第3档20%."""
    from app.core.position import position_manager

    position_manager.open_or_add("300750", "宁德时代", 100, 100.0, "首仓", None)
    position_manager.open_or_add("300750", "宁德时代", 60, 100.0, "加仓", None)
    plan = gen.generate("300750", "宁德时代", _signal(), portfolio={"total_pct": 65.0})
    assert plan["action"] == "buy_add"
    assert "第 3 档" in plan["content"]
    assert "20%" in plan["content"]


def test_plan_generate_stop(tmp_engine):
    from app.core.position import position_manager

    position_manager.open_or_add("000001", "平安银行", 100, 100.0, "首仓", None)
    _backdate("000001")  # 非当日买入 -> 正常生成止损计划
    sig = Signal(type="SELL_STOP", symbol="000001", name="平安银行", direction="sell",
                 strength=90.0, reason="跌破止损线", price=90.0)
    plan = gen.generate("000001", "平安银行", sig, portfolio={"total_pct": 20.0})
    assert plan["action"] == "sell_stop"
    assert "止损" in plan["content"]


def test_plan_generate_t_plus_one_blocks_sell(tmp_engine):
    """当日买入的持仓, 减仓/止损/做T卖出计划应转为持有提示(T+1)."""
    from app.core.position import position_manager

    # opened_at 默认为今日 -> 命中 T+1 锁定
    position_manager.open_or_add("000001", "平安银行", 100, 100.0, "首仓", None)
    for sig_type in ("SELL_REDUCE", "SELL_STOP", "T_SELL"):
        sig = Signal(type=sig_type, symbol="000001", name="平安银行", direction="sell",
                     strength=85.0, reason="测试", price=90.0)
        plan = gen.generate("000001", "平安银行", sig, portfolio={"total_pct": 20.0})
        assert plan["action"] == "hold", f"{sig_type} 应被 T+1 拦截为 hold"
        assert "T+1" in plan["content"]


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
