"""仓位管理单测(临时 DB)."""

from __future__ import annotations

import pytest
from app.core.config import config_manager
from app.core.fees import compute_trade_fee
from app.core.position.manager import PositionManager, PositionManagerError

pm = PositionManager()


def _fee(action: str, amount: float) -> float:
    """按当前费率配置算单笔手续费(不写死数值, 费率改了用例仍成立)."""
    return compute_trade_fee(action, amount, config_manager.get().get("手续费"))


def test_open_first_position(tmp_engine):
    pos = pm.open_or_add("300750", "宁德时代", 100, 100.0, "首仓", None)
    assert pos.qty == 100
    # cost 为含费摊薄成本(券商 APP 口径), cost_raw 为纯成交均价
    assert pos.cost == pytest.approx((100.0 * 100 + _fee("buy", 10000.0)) / 100)
    assert pos.cost > 100.0
    assert pos.cost_raw == pytest.approx(100.0)
    assert pos.status == "holding"


def test_pyramid_add_recalculates_cost(tmp_engine):
    pm.open_or_add("300750", "宁德时代", 100, 100.0, "首仓", None)
    pos = pm.open_or_add("300750", "宁德时代", 100, 120.0, "加仓", None)
    assert pos.qty == 200
    expected = (10000.0 + _fee("buy", 10000.0) + 12000.0 + _fee("buy", 12000.0)) / 200
    assert pos.cost == pytest.approx(expected, abs=1e-4)
    assert pos.cost_raw == pytest.approx(110.0)  # (100*100 + 120*100) / 200


def test_reject_downgrade_add(tmp_engine):
    pm.open_or_add("300750", "宁德时代", 100, 100.0, "首仓", None)
    with pytest.raises(PositionManagerError):
        pm.open_or_add("300750", "宁德时代", 100, 90.0, "低于成本加仓", None)


def test_flat_price_add_allowed(tmp_engine):
    """平价加仓不应被手续费误杀: 顺向判断走 cost_raw 而非含费成本."""
    pm.open_or_add("300750", "宁德时代", 100, 100.0, "首仓", None)
    pos = pm.open_or_add("300750", "宁德时代", 100, 100.0, "平价加仓", None)
    assert pos.qty == 200
    assert pos.cost_raw == pytest.approx(100.0)


def test_reduce_realized_pnl(tmp_engine):
    """已实现盈亏 = 现价 − 含费成本 − 卖出费, 即扣掉双边费用的净额."""
    pos = pm.open_or_add("300750", "宁德时代", 200, 100.0, "首仓", None)
    cost_incl = pos.cost  # 已摊入买入手续费
    pnl = pm.reduce("300750", 100, 120.0, "减仓", None)
    expected = round((120.0 - cost_incl) * 100, 2) - _fee("sell", 120.0 * 100)
    assert pnl == pytest.approx(expected, abs=0.01)
    # 严格小于毛盈亏(双边费用都被扣掉了)
    assert pnl < (120.0 - 100.0) * 100
    assert pm.get_position("300750", None).qty == 100


def test_close_position(tmp_engine):
    pos = pm.open_or_add("600519", "贵州茅台", 100, 100.0, "首仓", None)
    cost_incl = pos.cost
    pnl = pm.close("600519", 110.0, "清仓", None)
    expected = round((110.0 - cost_incl) * 100, 2) - _fee("sell", 110.0 * 100)
    assert pnl == pytest.approx(expected, abs=0.01)
    assert pm.get_position("600519", None) is None  # 已清仓不在持仓列表


def test_reduce_without_position_raises(tmp_engine):
    with pytest.raises(PositionManagerError):
        pm.reduce("000001", 100, 10.0, "test", None)


def test_pyramid_plan_stage(tmp_engine):
    pm.open_or_add("300750", "宁德时代", 100, 100.0, "首仓", None)
    plan = pm.pyramid_plan("300750", None)
    assert plan["strategy"] == "pyramid"
    assert plan["used_stage"] == 0  # 首仓后, 第一档(0.5)已用
    assert plan["suggest_next_pct"] == plan["remaining_ratios"][0] * 100


def test_take_profit_levels(tmp_engine):
    levels = pm.take_profit_levels(100.0, None)
    assert len(levels) == 3
    assert levels[0]["target_price"] == pytest.approx(103.0)
    assert levels[0]["target_pct"] == pytest.approx(3.0)


def test_kelly_discounted():
    f = pm.kelly(0.6, 2.0, 1.0)
    # f = 0.6 - 0.4/2 = 0.4, 折扣 0.5 -> 0.2
    assert f == pytest.approx(0.2)


def test_kelly_zero_for_bad_odds():
    assert pm.kelly(0.4, 1.0, 2.0) == 0.0  # 负期望 -> 0


def test_portfolio_summary(tmp_engine):
    pm.open_or_add("300750", "宁德时代", 100, 100.0, "首仓", None)
    pm.open_or_add("600519", "贵州茅台", 100, 200.0, "首仓", None)
    summary = pm.portfolio({"300750": 110.0, "600519": 200.0}, None)
    fees = _fee("buy", 10000.0) + _fee("buy", 20000.0)
    assert summary["market_value"] == pytest.approx(31000.0)
    # 成本含费 -> 浮盈比毛值少掉已付的买入手续费
    assert summary["unrealized_pnl"] == pytest.approx(1000.0 - fees, abs=0.02)
    assert summary["fee_cost"] == pytest.approx(fees, abs=0.02)
    assert summary["cost_raw_value"] == pytest.approx(30000.0)
    assert summary["cost_value"] == pytest.approx(30000.0 + fees, abs=0.02)
    assert len(summary["positions"]) == 2
